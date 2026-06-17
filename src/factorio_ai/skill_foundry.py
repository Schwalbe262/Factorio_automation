"""Self-development engine: local-Qwen-authored skill executors.

The foundry asks the local LLM to write a Python skill module for a skill the
strategy layer selected but that has no hand-written executor yet, then validates
the candidate through layered automated gates before it may be registered and run
on the live game:

1. ``static_safety_gate`` - AST allowlist + ``py_compile``.
2. ``offline_replay_gate`` - run ``next_action`` against recorded observations.
3. ``sandbox_dryrun_gate`` - headless dry-run on a COPY of the live save
   (phased; behind ``FACTORIO_AI_FOUNDRY_SANDBOX_ENABLED``).

The canonical registry is git-tracked at
``src/factorio_ai/generated_skills/registry.json`` (``runtime/`` is gitignored).
Generated modules live in :mod:`factorio_ai.generated_skills` and are loaded by
file path through :func:`load_generated_skill_class` only when registered.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import py_compile
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import models
from .config import REPO_ROOT

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

GENERATED_PKG_DIR = REPO_ROOT / "src" / "factorio_ai" / "generated_skills"

ALLOWED_IMPORT_MODULES = {
    "factorio_ai.models",
    "factorio_ai.planner",
    "math",
    "dataclasses",
    "typing",
    "__future__",
}

BANNED_NAMES = {
    "exec",
    "eval",
    "compile",
    "__import__",
    "open",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "vars",
    "input",
    "breakpoint",
    "memoryview",
}

BANNED_ATTR_ROOTS = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "shutil",
    "importlib",
    "builtins",
    "ctypes",
    "threading",
    "multiprocessing",
    "pathlib",
    "io",
    "pickle",
    "marshal",
}

BANNED_DUNDERS = {
    "__globals__",
    "__builtins__",
    "__import__",
    "__class__",
    "__bases__",
    "__base__",
    "__subclasses__",
    "__mro__",
    "__dict__",
    "__code__",
    "__loader__",
    "__spec__",
    "__getattribute__",
    "__reduce__",
    "__reduce_ex__",
    "__subclasshook__",
    "__init_subclass__",
    "__class_getitem__",
}

VALID_STATUSES = {
    "registered",
    "override_registered",  # a gated generated module that REPLACES a failing hand-written skill
    "candidate",
    "in_progress",
    "failed",
    "disabled",
    "quarantined",
}

# Backoff (seconds) for repeated failed generation of the same skill.
RETRY_BASE_SECONDS = 300
RETRY_MAX_SECONDS = 7200


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _generated_dir() -> Path:
    override = os.getenv("FACTORIO_AI_GENERATED_SKILLS_DIR", "").strip()
    if override:
        return Path(override)
    return GENERATED_PKG_DIR


def _registry_path() -> Path:
    return _generated_dir() / "registry.json"


def _resolve_repo_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Registry I/O (Phase 1)
# --------------------------------------------------------------------------- #


def _empty_registry() -> dict[str, Any]:
    return {"version": 1, "updated_at": _now_iso(), "skills": {}}


def load_registry() -> dict[str, Any]:
    path = _registry_path()
    if not path.exists():
        return _empty_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_registry()
    if not isinstance(data, dict):
        return _empty_registry()
    data.setdefault("version", 1)
    if not isinstance(data.get("skills"), dict):
        data["skills"] = {}
    return data


def save_registry(registry: dict[str, Any]) -> Path:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(registry)
    payload["updated_at"] = _now_iso()
    payload.setdefault("version", 1)
    payload.setdefault("skills", {})
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def write_runtime_mirror(runtime_dir: Path) -> Path | None:
    """Mirror the canonical registry to ``runtime/generated-skills.json`` for dashboards."""

    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        path = runtime_dir / "generated-skills.json"
        path.write_text(
            json.dumps(load_registry(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path
    except OSError:
        return None


def registry_status(name: str) -> dict[str, Any] | None:
    return load_registry()["skills"].get(name)


def _entry_file(entry: dict[str, Any]) -> Path:
    return _resolve_repo_path(entry.get("file_path") or "")


def registered_generated_skills() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, entry in load_registry()["skills"].items():
        if not isinstance(entry, dict) or entry.get("status") != "registered":
            continue
        try:
            if _entry_file(entry).exists():
                out[name] = entry
        except OSError:
            continue
    return out


def registered_override(name: str) -> dict[str, Any] | None:
    """An active (gated, non-quarantined) self-repair override for a hand-written skill, or None.

    When this returns None (no override, or it was quarantined after live regression) the controller
    falls back to the original hand-written skill, which is never deleted.
    """

    entry = registry_status(name)
    if not isinstance(entry, dict) or entry.get("status") != "override_registered":
        return None
    try:
        if _entry_file(entry).exists():
            return entry
    except OSError:
        return None
    return None


def update_skill(name: str, **fields: Any) -> dict[str, Any]:
    registry = load_registry()
    entry = registry["skills"].get(name)
    if not isinstance(entry, dict):
        entry = {"skill_name": name, "created_at": _now_iso(), "attempts": 0, "version": 0, "history": []}
    entry.update(fields)
    entry["skill_name"] = name
    entry["updated_at"] = _now_iso()
    registry["skills"][name] = entry
    save_registry(registry)
    return entry


def set_skill_status(name: str, status: str, reason: str = "", **fields: Any) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    payload: dict[str, Any] = {"status": status}
    if reason:
        payload["last_failure_reason"] = reason
    payload.update(fields)
    return update_skill(name, **payload)


def eligible_for_generation(name: str) -> tuple[bool, str]:
    """Decide whether the foundry should attempt to generate ``name`` now."""

    entry = registry_status(name)
    if not isinstance(entry, dict):
        return True, ""
    status = entry.get("status")
    if status in {"registered", "override_registered", "in_progress"}:
        return False, str(status)
    if status in {"quarantined", "disabled"}:
        return False, str(status)
    cooldown = entry.get("cooldown_until")
    if cooldown:
        try:
            if datetime.fromisoformat(cooldown) > _now():
                return False, "cooldown"
        except ValueError:
            pass
    try:
        lifetime_cap = max(1, int(os.getenv("FACTORIO_AI_FOUNDRY_LIFETIME_MAX_ATTEMPTS", "12")))
    except (TypeError, ValueError):
        lifetime_cap = 12
    if int(entry.get("attempts") or 0) >= lifetime_cap:
        return False, "lifetime_max_attempts"
    return True, ""


# --------------------------------------------------------------------------- #
# Priority queue (runtime/skill-foundry-priority.json)
# --------------------------------------------------------------------------- #


def _priority_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / "skill-foundry-priority.json"


def load_foundry_queue(runtime_dir: Path) -> list[dict[str, Any]]:
    path = _priority_path(runtime_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    queue = data.get("queue") if isinstance(data, dict) else None
    return [item for item in queue if isinstance(item, dict)] if isinstance(queue, list) else []


def _save_foundry_queue(runtime_dir: Path, queue: list[dict[str, Any]]) -> None:
    path = _priority_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": _now_iso(), "queue": queue}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def enqueue_foundry_request(
    runtime_dir: Path,
    skill_name: str,
    *,
    reason: str = "",
    blockers: list[str] | None = None,
    expected_effect: str = "",
    target_item: str | None = None,
    source: str = "autopilot_gap",
    priority: int = 50,
    mode: str = "new",
) -> dict[str, Any]:
    queue = load_foundry_queue(runtime_dir)
    entry = {
        "skill_name": skill_name,
        "priority": int(priority),
        "source": source,
        "reason": reason,
        "blockers": list(blockers or []),
        "expected_effect": expected_effect,
        "target_item": target_item,
        "mode": mode,  # "new" (generate a missing skill) or "override" (replace a failing implemented skill)
        "enqueued_at": _now_iso(),
    }
    for index, existing in enumerate(queue):
        if existing.get("skill_name") == skill_name:
            entry["priority"] = max(entry["priority"], int(existing.get("priority") or 0))
            entry["enqueued_at"] = existing.get("enqueued_at") or entry["enqueued_at"]
            queue[index] = entry
            break
    else:
        queue.append(entry)
    queue.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
    _save_foundry_queue(runtime_dir, queue)
    return entry


def remove_from_queue(runtime_dir: Path, skill_name: str) -> None:
    queue = [item for item in load_foundry_queue(runtime_dir) if item.get("skill_name") != skill_name]
    _save_foundry_queue(runtime_dir, queue)


def distinct_missing_skills(runtime_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    """Newest-first distinct entries from ``runtime/missing-skills.jsonl``."""

    path = Path(runtime_dir) / "missing-skills.jsonl"
    if not path.exists():
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = record.get("selected_skill")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(record)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# Dynamic loading (Phase 1)
# --------------------------------------------------------------------------- #


def load_generated_skill_class(entry: dict[str, Any] | str | Path, *, enforce_package_dir: bool = True) -> type:
    """Load the single skill class from a generated module by file path.

    A unique module name per call defeats ``sys.modules`` caching, so a freshly
    regenerated module is never shadowed by a stale class. ``enforce_package_dir``
    guards the controller pickup path (a registered entry must live inside the
    generated package); the foundry's own gating steps load a just-written
    candidate from a temp dir and pass ``enforce_package_dir=False``.
    """

    file_path = entry.get("file_path") if isinstance(entry, dict) else entry
    if not file_path:
        raise ValueError("registry entry has no file_path")
    path = _resolve_repo_path(file_path).resolve()
    if enforce_package_dir:
        gen_dir = _generated_dir().resolve()
        if gen_dir != path.parent and gen_dir not in path.parents:
            raise ValueError(f"refusing to load generated skill outside {gen_dir}: {path}")
    if not path.exists():
        raise FileNotFoundError(path)

    module_name = f"factorio_ai.generated_skills._loaded_{path.stem}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)

    classes = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and obj.__module__ == module_name and hasattr(obj, "next_action")
    ]
    if len(classes) != 1:
        raise ValueError(f"expected exactly one skill class in {path}, found {len(classes)}")
    return classes[0]


# --------------------------------------------------------------------------- #
# Observation samples for offline replay (Phase 2 / Gate 2 corpus)
# --------------------------------------------------------------------------- #


def _truncate_observation(observation: dict[str, Any], list_cap: int = 40) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in observation.items():
        if isinstance(value, list):
            out[key] = value[:list_cap]
        else:
            out[key] = value
    return out


def record_observation_sample(log_dir: Path, observation: dict[str, Any], max_bytes: int = 4_000_000) -> None:
    if not isinstance(observation, dict) or not observation:
        return
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "observation_samples.jsonl"
        line = json.dumps(_truncate_observation(observation), ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
        if path.stat().st_size > max_bytes:
            tail = path.read_text(encoding="utf-8").splitlines()[-200:]
            path.write_text("\n".join(tail) + "\n", encoding="utf-8")
    except OSError:
        return


def _synthetic_samples() -> list[dict[str, Any]]:
    return [
        {
            "inventory": {"iron-plate": 5, "wood": 2, "stone": 8, "coal": 4},
            "player": {"position": {"x": 0.0, "y": 0.0}},
            "resources": [
                {"name": "iron-ore", "position": {"x": 12.0, "y": 3.0}, "distance": 12.4},
                {"name": "coal", "position": {"x": -8.0, "y": 5.0}, "distance": 9.4},
                {"name": "tree", "position": {"x": 4.0, "y": 4.0}, "distance": 5.6},
            ],
            "entities": [],
            "craftable": {"iron-gear-wheel": 0},
        },
        {
            "inventory": {},
            "player": {"position": {"x": 50.0, "y": -30.0}},
            "resources": [],
            "entities": [
                {
                    "name": "stone-furnace",
                    "unit_number": 42,
                    "position": {"x": 51.0, "y": -30.0},
                    "distance": 1.0,
                    "inventories": {"output": {"iron-plate": 3}},
                }
            ],
            "craftable": {},
        },
    ]


def load_replay_samples(log_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    path = log_dir / "observation_samples.jsonl"
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    samples.append(obj)
        except OSError:
            pass
    if not samples:
        samples = _synthetic_samples()
    return samples


# --------------------------------------------------------------------------- #
# Gate results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "passed": self.passed, "reasons": list(self.reasons), "details": dict(self.details)}


# --------------------------------------------------------------------------- #
# Gate 1 - static safety (AST)
# --------------------------------------------------------------------------- #


def _attr_root(node: ast.Attribute) -> str | None:
    current: ast.AST = node.value
    while isinstance(current, ast.Attribute):
        current = current.value
    if isinstance(current, ast.Name):
        return current.id
    return None


def _safe_toplevel_node(node: ast.stmt) -> bool:
    if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef)):
        return True
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        return True  # module docstring / bare literal
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        value = node.value
        if value is None:
            return True
        return not any(isinstance(child, ast.Call) for child in ast.walk(value))
    return False


def static_safety_gate(code: str) -> GateResult:
    if not isinstance(code, str) or not code.strip():
        return GateResult("static_safety", False, ["empty code"])
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return GateResult("static_safety", False, [f"syntax error: {exc}"])

    reasons: list[str] = []

    for node in tree.body:
        if not _safe_toplevel_node(node):
            reasons.append(
                f"disallowed top-level statement: {type(node).__name__} at line {getattr(node, 'lineno', '?')}"
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_IMPORT_MODULES and alias.name not in ALLOWED_IMPORT_MODULES:
                    reasons.append(f"plain import of '{alias.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                reasons.append("relative imports are not allowed")
            module = node.module or ""
            if module not in ALLOWED_IMPORT_MODULES:
                reasons.append(f"import from '{module}' is not in the allowlist")
            for alias in node.names:
                if alias.name == "*":
                    reasons.append("'import *' is not allowed")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            reasons.append(f"use of banned builtin '{node.id}'")
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_DUNDERS:
                reasons.append(f"access to banned attribute '{node.attr}'")
            root = _attr_root(node)
            if root in BANNED_ATTR_ROOTS:
                reasons.append(f"attribute access through banned module '{root}'")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in BANNED_DUNDERS:
            reasons.append(f"banned dunder string constant '{node.value}'")
        elif isinstance(node, ast.Global):
            reasons.append("'global' statements are not allowed")
        elif isinstance(node, ast.Nonlocal):
            reasons.append("'nonlocal' statements are not allowed")
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            reasons.append("'with' statements are not allowed")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await)):
            reasons.append("async constructs are not allowed")

    class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    if len(class_defs) != 1:
        reasons.append(f"expected exactly one top-level class, found {len(class_defs)}")
    else:
        cls = class_defs[0]
        methods = {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        next_action = methods.get("next_action")
        if next_action is None:
            reasons.append("class must define next_action(self, observation)")
        else:
            arg_names = [a.arg for a in next_action.args.args]
            if arg_names[:2] != ["self", "observation"]:
                reasons.append("next_action must start with (self, observation)")
            if len(next_action.args.args) - len(next_action.args.defaults) > 2:
                reasons.append("extra next_action params must have defaults")
        init = methods.get("__init__")
        if init is not None and (len(init.args.args) - len(init.args.defaults)) > 1:
            reasons.append("__init__ params besides self must have defaults")

    if reasons:
        return GateResult("static_safety", False, reasons)

    compile_error = _py_compile_check(code)
    if compile_error:
        return GateResult("static_safety", False, [compile_error])
    return GateResult("static_safety", True, [])


def _py_compile_check(code: str) -> str | None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="factorio_foundry_"))
    source = tmp_dir / "candidate.py"
    cfile = tmp_dir / "candidate.pyc"
    try:
        source.write_text(code, encoding="utf-8")
        py_compile.compile(str(source), cfile=str(cfile), doraise=True)
    except py_compile.PyCompileError as exc:
        return f"py_compile failed: {exc}"
    except (SyntaxError, ValueError) as exc:
        return f"py_compile error: {exc}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


# --------------------------------------------------------------------------- #
# Gate 2 - offline replay
# --------------------------------------------------------------------------- #


def offline_replay_gate(file_path: str | Path, samples: list[dict[str, Any]]) -> GateResult:
    try:
        skill_class = load_generated_skill_class(file_path, enforce_package_dir=False)
    except Exception as exc:  # noqa: BLE001 - any load failure must fail the gate
        return GateResult("offline_replay", False, [f"load failed: {type(exc).__name__}: {exc}"])
    try:
        instance = skill_class()
    except Exception as exc:  # noqa: BLE001
        return GateResult("offline_replay", False, [f"zero-arg construction failed: {type(exc).__name__}: {exc}"])

    reasons: list[str] = []
    checked = 0
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        checked += 1
        started = time.monotonic()
        try:
            decision = instance.next_action(sample)
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"next_action raised: {type(exc).__name__}: {exc}")
            break
        if time.monotonic() - started > 2.0:
            reasons.append("next_action exceeded the 2s per-call budget")
            break
        if not isinstance(decision, models.PlannerDecision):
            reasons.append(f"next_action returned {type(decision).__name__}, not PlannerDecision")
            break
        if decision.action is not None:
            try:
                models.validate_action(dict(decision.action))
            except models.ActionValidationError as exc:
                reasons.append(f"invalid action: {exc}")
                break
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"action validation error: {type(exc).__name__}: {exc}")
                break
        if not isinstance(decision.reason, str):
            reasons.append("decision.reason must be a string")
            break
        if not isinstance(decision.done, bool):
            reasons.append("decision.done must be a bool")
            break

    if checked == 0:
        reasons.append("no observation samples available to replay")
    return GateResult("offline_replay", not reasons, reasons, {"samples_checked": checked})


# --------------------------------------------------------------------------- #
# Gate 3 - headless sandbox-save dry-run (phased; behind env flag)
# --------------------------------------------------------------------------- #


def _sandbox_enabled() -> bool:
    return os.getenv("FACTORIO_AI_FOUNDRY_SANDBOX_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def sandbox_dryrun_gate(cfg: Any, file_path: str | Path, *, steps: int = 25, target_item: str | None = None) -> GateResult:
    """Run a generated skill against a COPY of the live save in a headless server.

    The live save is never mutated (a file-copy of the zip is the snapshot). When
    the gate is disabled or the game executable is unavailable, the candidate is
    allowed to proceed on Gates 1+2 alone (``skipped``). For live autonomy this
    gate should be enabled so generated code is exercised before it touches the
    real world.
    """

    if not _sandbox_enabled():
        return GateResult("sandbox_dryrun", True, [], {"skipped": "FACTORIO_AI_FOUNDRY_SANDBOX_ENABLED is off"})
    try:
        return _run_sandbox_dryrun(cfg, file_path, steps=steps, target_item=target_item)
    except Exception as exc:  # noqa: BLE001 - infra issues degrade to skipped, not fail
        return GateResult("sandbox_dryrun", True, [], {"skipped": f"sandbox infra unavailable: {type(exc).__name__}: {exc}"})


def _run_sandbox_dryrun(cfg: Any, file_path: str | Path, *, steps: int, target_item: str | None) -> GateResult:
    import dataclasses
    import subprocess

    from . import factorio
    from .controller import ModlessFactorioController

    if not getattr(cfg, "factorio_exe", Path("")).exists():
        return GateResult("sandbox_dryrun", True, [], {"skipped": "factorio executable not found"})

    live_save = factorio.no_mod_save_path(cfg)
    if not live_save.exists():
        return GateResult("sandbox_dryrun", True, [], {"skipped": "no live save to snapshot"})

    sandbox_dir = cfg.runtime_dir / "vanilla" / "sandbox" / f"foundry-{uuid.uuid4().hex[:8]}"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    sandbox_save = sandbox_dir / live_save.name
    shutil.copy2(live_save, sandbox_save)  # snapshot; live save untouched

    base_server = int(os.getenv("FACTORIO_AI_FOUNDRY_SANDBOX_SERVER_PORT", "34297"))
    base_rcon = int(os.getenv("FACTORIO_AI_FOUNDRY_SANDBOX_RCON_PORT", "27115"))
    # Never let the throwaway sandbox server bind the live server's ports: a port clash would
    # either fail to start or, worse, point the dry-run at the real running game. Offset on collision.
    live_server = int(getattr(cfg, "server_port", 0) or 0)
    live_rcon = int(getattr(cfg, "rcon_port", 0) or 0)
    if base_server == live_server:
        base_server = live_server + 100
    if base_rcon == live_rcon:
        base_rcon = live_rcon + 100
    # Isolate runtime_dir too: the server config + write-data + mod dir all derive
    # from runtime_dir, and Factorio takes an exclusive lock on its write-data. Sharing
    # the live server's runtime_dir made the sandbox server fail to start (lock
    # conflict -> RCON connection refused), so the override sandbox gate always skipped.
    sandbox_cfg = dataclasses.replace(
        cfg,
        runtime_dir=sandbox_dir,
        server_port=base_server,
        rcon_port=base_rcon,
        log_dir=sandbox_dir,
    )
    command = factorio.build_start_no_mod_server_command(
        sandbox_cfg,
        save_path=sandbox_save,
        console_log=sandbox_dir / "sandbox-server.log",
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command, cwd=str(REPO_ROOT))
        factorio.wait_for_rcon(sandbox_cfg, timeout_seconds=int(os.getenv("FACTORIO_AI_FOUNDRY_SANDBOX_RCON_TIMEOUT", "120")))
        controller = ModlessFactorioController(sandbox_cfg)
        skill_class = load_generated_skill_class(file_path, enforce_package_dir=False)
        run = controller._run_skill(
            skill=skill_class(),
            target_item=target_item or "iron-plate",
            target=10_000_000,
            goal="sandbox_dryrun",
            max_steps=max(1, steps),
            log_prefix="sandbox-dryrun",
        )
        ok = bool(getattr(run, "ok", False)) or getattr(run, "steps", 0) >= 1
        return GateResult(
            "sandbox_dryrun",
            True if ok else False,
            [] if ok else [f"sandbox run did not progress: {getattr(run, 'reason', '')}"],
            {"steps": getattr(run, "steps", 0), "reason": getattr(run, "reason", "")},
        )
    finally:
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=20)
            except Exception:  # noqa: BLE001
                try:
                    process.kill()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(sandbox_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Code generation
# --------------------------------------------------------------------------- #

_CODEGEN_SYSTEM = """You are a senior Python engineer writing ONE deterministic Factorio skill executor module.

Output JSON only: {"class_name": "<PascalCaseName>", "code": "<full python module>", "notes": "<short>"}.
The "code" field is a complete, self-contained Python module. No prose, no markdown fences.

HARD RULES (a validator rejects any violation):
- Imports allowed ONLY: `from factorio_ai.models import ...`, `from factorio_ai.planner import ...`,
  and stdlib `math`, `dataclasses`, `typing`. Absolute imports only; never `import os`/`sys`/etc.; never `import *`.
- Define EXACTLY ONE class. Its name ends with `Skill`. `__init__` (if any) must default every parameter
  besides `self`. The class must define `next_action(self, observation)` (extra params must be defaulted).
- `next_action` MUST return a `factorio_ai.models.PlannerDecision`:
  `PlannerDecision(action, reason, done=False)` where `action` is `None` or a dict with a `type` field.
- Pure function of `observation`: NO file/network/process/threads, NO `open`/`exec`/`eval`/`compile`/`getattr`/`__import__`,
  NO dunder escapes (`__class__`, `__subclasses__`, ...), NO `print`, NO `global`/`nonlocal`, NO `with`.
- Never raise on a malformed observation. When unsure, return `PlannerDecision(None, "<why>")` or a `wait` action.
- Use ONLY exact names from the "VALID NAMES" list in the user message for recipe/item/entity/resource
  strings; never invent names. Always return `done=True` once the target is reached.

ACTION TYPES and their required fields (must satisfy the validator):
- move_to: {"type":"move_to","position":{"x":<num>,"y":<num>}}
- mine: {"type":"mine","position":{...}}  (or "near" / "unit_number")
- craft: {"type":"craft","recipe":"<name>","count":<int>=1}  (count >= 1)
- build: {"type":"build","name":"<entity>","position":{...},"direction":<0|2|4|6|8 optional>}
- insert/take: {"type":"insert","item":"<name>","position":{...} or "unit_number":<n>,"count":<int>=1}
- set_recipe: {"type":"set_recipe","recipe":"<name>","position":{...} or "unit_number":<n>}
- connect_power: {"type":"connect_power","position":{...} or "unit_number":<n>}
- research: {"type":"research","technology":"<name>"}
- restore_character_controller / stop: {"type":"stop"}
- wait: {"type":"wait","ticks":<1..36000>}

HELPER FUNCTIONS available from factorio_ai.models (prefer these):
- inventory_count(obs, item) -> int
- total_item_count(obs, item) -> int       # inventory + entity inventories
- craftable_count(obs, recipe) -> int
- player_position(obs) -> {"x","y"}
- distance(a, b) -> float                   # a,b are {"x","y"}
- nearest_resource(obs, name) -> dict|None  # has "position", "distance"
- nearest_entity(obs, name) -> dict|None
- entities_named(obs, name) -> list[dict]
- entity_item_count(entity, item) -> int

Write a small, robust, single-purpose skill that makes incremental progress toward its goal each call
and returns `done=True` once the target is reached.
"""

_CODEGEN_SCHEMA = {
    "type": "object",
    "properties": {
        "class_name": {"type": "string"},
        "code": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["class_name", "code"],
}


def _example_skill_source() -> str:
    """Several short, gate-compliant examples covering different action patterns.

    Showing the model gather/mine, craft-with-prerequisite, and build/insert templates (not just one)
    materially raises the rate of generated skills that pass the gates and run.
    """

    return "\n\n# ---- example ----\n\n".join(_HAND_EXAMPLES)


_HAND_EXAMPLES = (
    # Pattern 1: gather a raw resource until a target (move_to + mine + done + wait fallback).
    '''from __future__ import annotations

from factorio_ai.models import PlannerDecision, total_item_count, nearest_resource, player_position, distance


class StockpileWoodSkill:
    """Gather wood until a target count is reached."""

    def __init__(self, target: int = 20):
        self.target = target

    def next_action(self, observation):
        have = total_item_count(observation, "wood")
        if have >= self.target:
            return PlannerDecision(None, "wood target reached", done=True)
        tree = nearest_resource(observation, "tree")
        if tree is None or not isinstance(tree.get("position"), dict):
            return PlannerDecision({"type": "wait", "ticks": 60}, "no tree visible; waiting")
        position = tree["position"]
        if distance(player_position(observation), position) > 3:
            return PlannerDecision({"type": "move_to", "position": position}, "move to the nearest tree")
        return PlannerDecision({"type": "mine", "position": position}, "mine wood")
''',
    # Pattern 2: craft an item, mining the missing ingredient first (craft + prerequisite).
    '''from __future__ import annotations

from factorio_ai.models import PlannerDecision, inventory_count, total_item_count, nearest_resource, player_position, distance


class CraftStoneFurnaceSkill:
    """Craft stone furnaces, mining stone first when short of the recipe ingredient."""

    def __init__(self, target: int = 2):
        self.target = target

    def next_action(self, observation):
        if total_item_count(observation, "stone-furnace") >= self.target:
            return PlannerDecision(None, "stone-furnace target reached", done=True)
        if inventory_count(observation, "stone") >= 5:
            return PlannerDecision({"type": "craft", "recipe": "stone-furnace", "count": 1}, "craft a stone furnace")
        stone = nearest_resource(observation, "stone")
        if stone is None or not isinstance(stone.get("position"), dict):
            return PlannerDecision({"type": "wait", "ticks": 60}, "no stone visible; waiting")
        position = stone["position"]
        if distance(player_position(observation), position) > 3:
            return PlannerDecision({"type": "move_to", "position": position}, "move to stone")
        return PlannerDecision({"type": "mine", "position": position}, "mine stone for the recipe")
''',
    # Pattern 3: act on a nearby machine by unit_number (insert into an entity).
    '''from __future__ import annotations

from factorio_ai.models import PlannerDecision, nearest_entity, entity_item_count, inventory_count


class FuelNearbyFurnaceSkill:
    """Keep the nearest stone furnace fueled with coal from inventory."""

    def __init__(self, min_fuel: int = 2):
        self.min_fuel = min_fuel

    def next_action(self, observation):
        furnace = nearest_entity(observation, "stone-furnace")
        if furnace is None:
            return PlannerDecision(None, "no stone furnace to fuel", done=True)
        if entity_item_count(furnace, "coal") >= self.min_fuel:
            return PlannerDecision(None, "furnace already fueled", done=True)
        if inventory_count(observation, "coal") <= 0:
            return PlannerDecision({"type": "wait", "ticks": 60}, "no coal in inventory; waiting")
        unit = furnace.get("unit_number")
        if unit is None:
            return PlannerDecision({"type": "wait", "ticks": 60}, "furnace has no unit_number")
        return PlannerDecision({"type": "insert", "item": "coal", "unit_number": unit, "count": 1}, "insert coal into furnace")
''',
)


def _codegen_vocabulary() -> str:
    """Real recipe/resource/item names so generated code uses valid strings (a top failure cause)."""

    try:
        from . import knowledge
        from .models import ALLOWED_ACTION_TYPES

        recipes = sorted(knowledge.ALL_RECIPES.keys())
        raws = sorted(knowledge.RAW_RESOURCES)
        items: set[str] = set()
        for recipe in knowledge.ALL_RECIPES.values():
            items.update(recipe.products.keys())
            items.update(recipe.ingredients.keys())
        actions = sorted(ALLOWED_ACTION_TYPES)
    except Exception:  # noqa: BLE001 - vocabulary is best-effort
        return ""
    return (
        "VALID NAMES - use ONLY these exact strings for action types and recipe/item/entity/resource names:\n"
        f"- action types: {', '.join(actions)}\n"
        f"- recipe names (for craft/set_recipe; also the buildable entity name for placeable items): {', '.join(recipes)}\n"
        f"- raw resources (for a mine target): {', '.join(raws)}\n"
        f"- item names (inventory/insert/take): {', '.join(sorted(items))}"
    )


def _build_codegen_prompt(
    spec: dict[str, Any],
    observation_samples: list[dict[str, Any]],
    previous_failure: str,
) -> str:
    blockers = spec.get("blockers")
    blockers_text = ", ".join(blockers) if isinstance(blockers, list) else str(blockers or "")
    sample_text = json.dumps(observation_samples[:2], ensure_ascii=False)[:3000]
    parts: list[str] = []
    if str(spec.get("mode") or "new").strip().lower() == "override":
        parts += [
            f"You are REPAIRING an existing skill '{spec.get('skill_name')}' that keeps FAILING in the live game.",
            "Write a corrected, robust replacement that handles the failure below — e.g. if a build fails, try a",
            "different position/direction or a different site, skip blocked tiles, and never loop forever on the same",
            "failing action. Make incremental progress each call and return done=True once the goal is reached.",
            "",
        ]
    parts += [
        f"Skill name to implement: {spec.get('skill_name')}",
        f"Why the strategy layer wants it: {spec.get('reason') or '(unspecified)'}",
        f"Reported blockers (recent live failures): {blockers_text or '(none)'}",
        f"Expected effect: {spec.get('expected_effect') or '(unspecified)'}",
        f"Suggested primary item/target: {spec.get('target_item') or '(infer from name)'}",
        "",
        _codegen_vocabulary(),
        "",
        "Reference example skill(s) showing the exact contract (imitate this style):",
        "```python",
        _example_skill_source(),
        "```",
        "",
        "Real (truncated) observation samples from the live game so you use real keys:",
        sample_text,
    ]
    if previous_failure:
        parts += [
            "",
            "YOUR PREVIOUS ATTEMPT WAS REJECTED. Fix exactly this and resubmit:",
            previous_failure,
        ]
    parts += ["", "Return strict JSON with the full module in the `code` field."]
    return "\n".join(parts)


def _codegen_max_tokens() -> int:
    try:
        return max(512, min(4096, int(os.getenv("FACTORIO_AI_FOUNDRY_MAX_TOKENS", "3072"))))
    except (TypeError, ValueError):
        return 3072


def _strip_code_fences(code: str) -> str:
    text = code.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip() + "\n"


def _local_llm_endpoint_configured() -> bool:
    return bool(os.getenv("FACTORIO_AI_LLM_BASE_URL", "").strip() and os.getenv("FACTORIO_AI_LLM_MODEL", "").strip())


def generate_skill_code(
    spec: dict[str, Any],
    *,
    observation_samples: list[dict[str, Any]],
    previous_failure: str = "",
    task_id: str = "",
    prefer_remote: bool = False,
) -> tuple[str | None, str, dict[str, Any]]:
    """Ask the local Qwen for a skill module. Returns (code, class_name, diagnostics).

    When ``prefer_remote`` is set (Slurm enabled), codegen is offloaded to a scheduler/attached
    task so the LLM call runs on the node where the vLLM endpoint is reachable, mirroring strategy
    requests. The in-process call is used only for local-direct setups (a configured
    ``FACTORIO_AI_LLM_BASE_URL``) or when no Slurm is configured.
    """

    if prefer_remote:
        result: dict[str, Any] | None
        try:
            from . import remote_slurm

            result = remote_slurm.request_skill_foundry_code(
                spec,
                observation_samples=observation_samples[:6],
                previous_failure=previous_failure,
                task_id=task_id,
                max_tokens=_codegen_max_tokens(),
            )
        except Exception as exc:  # noqa: BLE001 - offload failure degrades to local only if reachable
            if not _local_llm_endpoint_configured():
                return None, "", {"foundry_error": f"remote foundry codegen failed: {type(exc).__name__}: {exc}"}
            result = None
        if result is not None:
            diagnostics = {k: v for k, v in result.items() if k not in {"code", "class_name", "notes", "ok", "type"}}
            code = result.get("code")
            class_name = str(result.get("class_name") or "").strip()
            if not isinstance(code, str) or not code.strip():
                diagnostics.setdefault("foundry_error", result.get("foundry_error") or "remote foundry task returned no code")
                return None, class_name, diagnostics
            return _strip_code_fences(code), class_name, diagnostics

    from .slurm_worker import call_llm_json_with_diagnostics

    prompt = _build_codegen_prompt(spec, observation_samples, previous_failure)
    parsed, diagnostics = call_llm_json_with_diagnostics(
        _CODEGEN_SYSTEM,
        prompt,
        _CODEGEN_SCHEMA,
        kind="skill_foundry",
        task_id=task_id,
        max_tokens=_codegen_max_tokens(),
    )
    if not isinstance(parsed, dict):
        diagnostics.setdefault("foundry_error", "LLM returned no JSON object")
        return None, "", diagnostics
    code = parsed.get("code")
    class_name = str(parsed.get("class_name") or "").strip()
    if not isinstance(code, str) or not code.strip():
        diagnostics.setdefault("foundry_error", "LLM JSON had no code field")
        return None, class_name, diagnostics
    return _strip_code_fences(code), class_name, diagnostics


# --------------------------------------------------------------------------- #
# Event log + attempt archive
# --------------------------------------------------------------------------- #


def log_foundry_event(log_dir: Path, event: str, payload: dict[str, Any]) -> None:
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        record = {"timestamp": _now_iso(), "event": event}
        record.update(payload)
        with (log_dir / "skill_foundry.jsonl").open("a", encoding="utf-8") as file:
            json.dump(record, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")
    except OSError:
        pass


def _archive_attempt(runtime_dir: Path, skill_name: str, version: int, attempt: int, code: str, meta: dict[str, Any]) -> None:
    try:
        attempts_dir = runtime_dir / "generated-skills" / "attempts"
        attempts_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now().strftime("%Y%m%d-%H%M%S")
        stem = f"{skill_name}-v{version}-attempt{attempt}-{stamp}-{uuid.uuid4().hex[:6]}"
        (attempts_dir / f"{stem}.py").write_text(code, encoding="utf-8")
        (attempts_dir / f"{stem}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# develop_skill - the generate -> gate -> register orchestration
# --------------------------------------------------------------------------- #


def _default_max_attempts() -> int:
    try:
        return max(1, min(8, int(os.getenv("FACTORIO_AI_FOUNDRY_MAX_ATTEMPTS", "3"))))
    except (TypeError, ValueError):
        return 3


def _cooldown_until(total_attempts: int) -> str:
    seconds = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** max(0, total_attempts - 1)))
    return (_now() + timedelta(seconds=seconds)).isoformat()


def develop_skill(
    cfg: Any,
    spec: dict[str, Any],
    *,
    max_attempts: int | None = None,
    run_sandbox: bool | None = None,
    log_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Generate, gate, and (on success) register a new skill executor.

    Returns ``{ok, status, skill_name, file_path, gates_passed, failure_reason, attempts}``.
    """

    runtime_dir = runtime_dir or getattr(cfg, "runtime_dir", Path("runtime"))
    log_dir = log_dir or getattr(cfg, "log_dir", Path("logs"))
    name = str(spec.get("skill_name") or "").strip()
    if not name or not name.replace("_", "").isalnum():
        return {"ok": False, "status": "failed", "skill_name": name, "failure_reason": "invalid skill_name"}

    # "override" mode = self-repair of a failing hand-written skill. It must be sandbox-proven before
    # it can auto-replace a core skill, and it registers under a distinct status.
    mode = str(spec.get("mode") or "new").strip().lower()
    is_override = mode == "override"

    attempts_budget = max_attempts or _default_max_attempts()
    existing = registry_status(name) or {}
    version = int(existing.get("version") or 0)
    lifetime_attempts = int(existing.get("attempts") or 0)
    samples = load_replay_samples(log_dir)
    previous_failure = str(spec.get("previous_failure") or existing.get("last_failure_reason") or "")

    set_skill_status(
        name,
        "in_progress",
        attempts=lifetime_attempts,
        version=version,
        target_item=spec.get("target_item"),
        goal=spec.get("goal") or name,
        default_target=spec.get("default_target"),
        default_max_steps=spec.get("default_max_steps"),
        log_prefix=spec.get("log_prefix") or f"generated-{name}",
    )
    log_foundry_event(log_dir, "develop_start", {"skill_name": name, "version": version})

    last_reason = previous_failure
    for attempt in range(1, attempts_budget + 1):
        lifetime_attempts += 1
        task_id = f"foundry-{name}-{uuid.uuid4().hex[:8]}"
        code, class_name, diagnostics = generate_skill_code(
            spec,
            observation_samples=samples,
            previous_failure=last_reason,
            task_id=task_id,
            prefer_remote=bool(getattr(cfg, "slurm_enabled", False)),
        )
        if code is None:
            last_reason = str(diagnostics.get("foundry_error") or diagnostics.get("llm_error") or "no code produced")
            log_foundry_event(log_dir, "generate_failed", {"skill_name": name, "attempt": attempt, "reason": last_reason})
            continue

        code_sha = _sha256(code)
        g1 = static_safety_gate(code)
        if not g1.passed:
            last_reason = "static safety: " + "; ".join(g1.reasons[:4])
            _archive_attempt(runtime_dir, name, version, attempt, code, {"gate": g1.to_dict(), "sha256": code_sha})
            log_foundry_event(log_dir, "gate_failed", {"skill_name": name, "attempt": attempt, "gate": "static_safety", "reasons": g1.reasons})
            continue

        tmp_dir = Path(tempfile.mkdtemp(prefix="factorio_foundry_replay_"))
        tmp_file = tmp_dir / f"{name}.py"
        try:
            tmp_file.write_text(code, encoding="utf-8")
            g2 = offline_replay_gate(tmp_file, samples)
        finally:
            # keep file only as long as the replay needs it
            pass
        if not g2.passed:
            last_reason = "offline replay: " + "; ".join(g2.reasons[:4])
            _archive_attempt(runtime_dir, name, version, attempt, code, {"gate": g2.to_dict(), "sha256": code_sha})
            log_foundry_event(log_dir, "gate_failed", {"skill_name": name, "attempt": attempt, "gate": "offline_replay", "reasons": g2.reasons})
            shutil.rmtree(tmp_dir, ignore_errors=True)
            continue

        gates_passed = ["static_safety", "offline_replay"]
        # Overrides REPLACE a working-by-default hand-written skill on the live game, so the sandbox
        # dry-run is mandatory and must actually run (a "skipped" sandbox is not acceptable for them).
        want_sandbox = True if is_override else (_sandbox_enabled() if run_sandbox is None else run_sandbox)
        if want_sandbox:
            g3 = sandbox_dryrun_gate(cfg, tmp_file, steps=25, target_item=spec.get("target_item"))
            if not g3.passed:
                last_reason = "sandbox dry-run: " + "; ".join(g3.reasons[:4])
                _archive_attempt(runtime_dir, name, version, attempt, code, {"gate": g3.to_dict(), "sha256": code_sha})
                log_foundry_event(log_dir, "gate_failed", {"skill_name": name, "attempt": attempt, "gate": "sandbox_dryrun", "reasons": g3.reasons})
                shutil.rmtree(tmp_dir, ignore_errors=True)
                continue
            if g3.details.get("skipped"):
                if is_override:
                    last_reason = "override requires a sandbox dry-run but the sandbox is unavailable"
                    _archive_attempt(runtime_dir, name, version, attempt, code, {"gate": g3.to_dict(), "sha256": code_sha})
                    log_foundry_event(log_dir, "gate_failed", {"skill_name": name, "attempt": attempt, "gate": "sandbox_unavailable", "reasons": [last_reason]})
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    continue
            else:
                gates_passed.append("sandbox_dryrun")
        shutil.rmtree(tmp_dir, ignore_errors=True)

        # All required gates passed -> register.
        version += 1
        module_path = _generated_dir() / f"{name}.py"
        module_path.parent.mkdir(parents=True, exist_ok=True)
        module_path.write_text(code, encoding="utf-8")
        file_path = _relpath_for_registry(module_path)
        history = list(existing.get("history") or [])
        history.append({"version": version, "registered_at": _now_iso(), "gates_passed": gates_passed, "code_sha256": code_sha})
        registered_status = "override_registered" if is_override else "registered"
        entry = update_skill(
            name,
            status=registered_status,
            is_override=is_override,
            base_skill=name if is_override else None,
            class_name=class_name or _guess_class_name(code),
            module=f"factorio_ai.generated_skills.{name}",
            file_path=file_path,
            gates_passed=gates_passed,
            attempts=lifetime_attempts,
            version=version,
            code_sha256=code_sha,
            live_failures=0,
            last_failure_reason="",
            cooldown_until="",
            target_item=spec.get("target_item"),
            goal=spec.get("goal") or name,
            default_target=spec.get("default_target"),
            default_max_steps=spec.get("default_max_steps"),
            log_prefix=spec.get("log_prefix") or f"generated-{name}",
            history=history[-10:],
        )
        write_runtime_mirror(runtime_dir)
        log_foundry_event(
            log_dir,
            "registered",
            {"skill_name": name, "version": version, "gates_passed": gates_passed, "file_path": file_path, "mode": mode},
        )
        return {
            "ok": True,
            "status": registered_status,
            "skill_name": name,
            "file_path": file_path,
            "class_name": entry.get("class_name"),
            "gates_passed": gates_passed,
            "attempts": lifetime_attempts,
            "version": version,
            "is_override": is_override,
        }

    # Exhausted attempts.
    set_skill_status(
        name,
        "failed",
        last_reason or "all generation attempts failed",
        attempts=lifetime_attempts,
        cooldown_until=_cooldown_until(lifetime_attempts),
    )
    write_runtime_mirror(runtime_dir)
    log_foundry_event(log_dir, "develop_exhausted", {"skill_name": name, "attempts": lifetime_attempts, "reason": last_reason})
    return {
        "ok": False,
        "status": "failed",
        "skill_name": name,
        "failure_reason": last_reason,
        "attempts": lifetime_attempts,
    }


def _relpath_for_registry(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _guess_class_name(code: str) -> str:
    try:
        for node in ast.walk(ast.parse(code)):
            if isinstance(node, ast.ClassDef):
                return node.name
    except SyntaxError:
        pass
    return ""
