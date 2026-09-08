"""Restart-safe state for the deterministic controller, independent of model services.

The process lock protects a run's checkpoint and append-only event journal. Completion
is cached evidence only: the supervisor must still check live completion predicates.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any


class TaskStatus(str, Enum):
    RUNNING = "running"
    WAITING = "waiting"
    BLOCKED = "blocked"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True)
class TaskResult:
    status: TaskStatus | str
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    failure_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", TaskStatus(self.status))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class TaskProgress:
    status: str = TaskStatus.RUNNING.value
    progress_key: str = ""
    last_progress_tick: int = 0
    last_observed_tick: int = 0
    failure_counts: dict[str, int] = field(default_factory=dict)
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunState:
    world_id: str
    game_fingerprint: str
    last_tick: int = 0
    tasks: dict[str, TaskProgress] = field(default_factory=dict)
    generation: int = 0
    invalidation_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.world_id or not self.game_fingerprint:
            raise ValueError("world_id and game_fingerprint are required")
        if self.last_tick < 0:
            raise ValueError("tick must be non-negative")

    def reconcile(self, world_id: str, game_fingerprint: str, tick: int) -> bool:
        """Invalidate task proofs on world/data changes or a restored older game tick."""
        if not world_id or not game_fingerprint or tick < 0:
            raise ValueError("valid world_id, game_fingerprint and tick are required")
        reason = None
        if world_id != self.world_id:
            reason = "world_changed"
        elif game_fingerprint != self.game_fingerprint:
            reason = "game_fingerprint_changed"
        elif tick < self.last_tick:
            reason = "game_tick_rolled_back"
        if reason:
            self.tasks.clear()
            self.generation += 1
            self.invalidation_reason = reason
        self.world_id, self.game_fingerprint, self.last_tick = world_id, game_fingerprint, tick
        return reason is not None

    def record_task(
        self, goal_id: str, result: TaskResult, *, tick: int,
        progress: Any = None, max_retries: int = 3,
    ) -> TaskResult:
        """Record only goal-specific evidence, bounding repeated failures in that state.

        ``progress`` must represent advancement of this goal; unrelated factory stock
        must not be included. Retry history survives executor/failure-code oscillation.
        """
        if not goal_id or max_retries < 1:
            raise ValueError("goal_id is required and max_retries must be positive")
        self.reconcile(self.world_id, self.game_fingerprint, tick)
        key = _fingerprint(progress)
        task = self.tasks.get(goal_id)
        if task is None:
            task = TaskProgress(last_progress_tick=tick)
            self.tasks[goal_id] = task
        if task.progress_key != key:
            task.progress_key = key
            task.last_progress_tick = tick
            task.failure_counts.clear()
        if result.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
            failure_key = _fingerprint([key, result.failure_code or result.reason])
            count = task.failure_counts.get(failure_key, 0) + 1
            task.failure_counts[failure_key] = count
            if count >= max_retries:
                result = TaskResult(
                    TaskStatus.BLOCKED, f"retry budget exhausted: {result.reason}",
                    {**result.evidence, "attempts": count, "retry_budget": max_retries,
                     "original_failure_code": result.failure_code},
                    "retry_budget_exhausted",
                )
        task.last_observed_tick = tick
        task.status, task.reason, task.evidence = result.status.value, result.reason, dict(result.evidence)
        return result

    def is_stalled(self, goal_id: str, *, tick: int, max_stall_ticks: int) -> bool:
        if max_stall_ticks < 1:
            raise ValueError("max_stall_ticks must be positive")
        task = self.tasks.get(goal_id)
        return bool(task and task.status != TaskStatus.SUCCEEDED.value
                    and tick >= task.last_observed_tick
                    and tick - task.last_progress_tick >= max_stall_ticks)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, **asdict(self)}


class CheckpointError(ValueError):
    pass


def load_run_state(path: Path, *, world_id: str, game_fingerprint: str, tick: int) -> RunState:
    path = Path(path)
    if not path.exists():
        return RunState(world_id, game_fingerprint, tick)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported checkpoint schema")
        tasks = {name: TaskProgress(**data) for name, data in raw["tasks"].items()}
        for task in tasks.values():
            TaskStatus(task.status)
        state = RunState(raw["world_id"], raw["game_fingerprint"], raw["last_tick"], tasks,
                         raw.get("generation", 0), raw.get("invalidation_reason"))
        state.reconcile(world_id, game_fingerprint, tick)
        return state
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CheckpointError(f"cannot load deterministic checkpoint {path}: {exc}") from exc


def replace_with_retry(source: str | Path, destination: str | Path) -> None:
    """Windows readers can briefly deny rename while reading a status snapshot."""
    for attempt in range(6):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.01 * 2 ** attempt)


def _atomic_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        replace_with_retry(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_run_state(path: Path, state: RunState) -> None:
    _atomic_json(path, state.to_dict())


def append_task_event(path: Path, state: RunState, goal_id: str, result: TaskResult) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "world_id": state.world_id,
               "game_fingerprint": state.game_fingerprint, "generation": state.generation,
               "tick": state.last_tick, "goal_id": goal_id, "result": result.to_dict()}
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
        output.flush()
        os.fsync(output.fileno())


class RunAlreadyOwnedError(RuntimeError):
    pass


class RunLock:
    """Nonblocking OS lock; dead processes release ownership automatically.

    The persistent file is never removed: unlinking a locked file lets another
    process lock a different inode. PID metadata is diagnostic, never authority;
    this also avoids PID reuse and Windows process-permission races.
    """
    def __init__(self, path: Path):
        self.path = Path(path)
        self._file: Any = None

    def acquire(self) -> "RunLock":
        if self._file is not None:
            raise RunAlreadyOwnedError("this owner already holds the run lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(fd, "r+b", buffering=0)
        try:
            # Lock one byte, including beyond EOF; never write before acquiring.
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RunAlreadyOwnedError(f"another process owns run lock: {self.path}") from exc
        self._file = handle
        try:
            handle.write(json.dumps({"pid": os.getpid(), "acquired_at": datetime.now(timezone.utc).isoformat()}).encode())
            handle.truncate()
            os.fsync(handle.fileno())
        except BaseException:
            self.release()
            raise
        return self

    def release(self) -> None:
        if self._file is not None:
            handle, self._file = self._file, None
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *args: Any) -> None:
        self.release()


def request_stop(path: Path) -> None:
    _atomic_json(Path(path), {"requested_at": datetime.now(timezone.utc).isoformat()})


def stop_requested(path: Path) -> bool:
    return Path(path).exists()


def clear_stop(path: Path) -> None:
    Path(path).unlink(missing_ok=True)
