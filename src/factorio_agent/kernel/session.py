"""Run the typed commands of ``python -m factorio_agent`` inside the supervisor process.

The supervisor used to start a new Python process for every observation, status
read and execution slice. ``Session`` keeps what those processes rebuilt each
time (the RCON socket and the parsed catalog) and ``InProcessBridge`` exposes it
through the same ``invoke`` / ``validate_requests`` interface as ``CLIBridge``.

Executors are still rebuilt for every ``run`` command. They load controller
state files when constructed, and other commands may rewrite those files.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import traceback

from .. import runtime
from ..__main__ import execute
from ..jobs import Request, StarterExecutors

# Commands that only read. Their receipts may be reused for a moment.
READ_ONLY = frozenset({"observe", "status", "capabilities", "actor-status", "belongings-status"})
# Commands that touch only marker files. They must work while a slice is running.
LOCK_FREE = frozenset({"stop-worker", "clear-worker-stop"})
# Receipt errors the one-shot CLI reported as {"status": "error"}.
TYPED_ERRORS = (ValueError, RuntimeError, FileNotFoundError, FileExistsError)
OBSERVE_REUSE_SECONDS = 2.0


class Session:
    """Shared game access for one runtime directory."""

    def __init__(self, root, *, clock=time.monotonic):
        self.root = Path(root).resolve()
        self.clock = clock
        self._lock = threading.Lock()
        self._game = None
        self._catalog = None
        self._catalog_stamp = None
        self._observation = None  # (taken_at, receipt)
        self.calls: dict[str, dict[str, float]] = {}

    # --- services used by __main__.execute -------------------------------

    def game(self, root):
        if self._game is None:
            game = runtime.game_for(self.root)
            game.keep_connection = True
            self._game = game
        return self._game

    def catalog(self, root, game):
        path = self.root / "catalog.json"
        if not path.exists():
            return runtime.catalog_for(self.root, game)
        stamp = path.stat().st_mtime_ns
        if self._catalog is None or stamp != self._catalog_stamp:
            self._catalog = runtime.catalog_for(self.root, game)
            self._catalog_stamp = stamp
        return self._catalog

    def executors(self, root, game, catalog):
        return StarterExecutors(game, catalog)

    # --- command execution -----------------------------------------------

    def run(self, args):
        """Execute one command and return its receipt as plain JSON data."""
        command = args[0]
        if command in LOCK_FREE:
            return self._timed(command, args)
        with self._lock:
            if command == "observe":
                cached = self._observation
                if cached and self.clock() - cached[0] < OBSERVE_REUSE_SECONDS:
                    self._count("observe-reused", 0.0)
                    return json.loads(cached[1])
            elif command not in READ_ONLY:
                # Anything that can change the world or the ledger ends reuse.
                self._observation = None
            receipt = self._timed(command, args)
            if command == "observe":
                self._observation = (self.clock(), json.dumps(receipt, allow_nan=False))
            return receipt

    def _timed(self, command, args):
        started = time.perf_counter()
        try:
            result = execute(["--runtime", str(self.root), *args], services=self)
            # A receipt crosses the same boundary as CLI stdout: plain JSON only.
            return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
        except BaseException:
            if command not in READ_ONLY | LOCK_FREE:
                self._observation = None
            raise
        finally:
            self._count(command, time.perf_counter() - started)

    def _count(self, command, seconds):
        row = self.calls.setdefault(command, {"count": 0, "seconds": 0.0})
        row["count"] += 1
        row["seconds"] += seconds

    def metrics(self):
        """Totals since the session started: per-command time and RCON query time."""
        queries = getattr(self._game, "query_stats", None) or {"count": 0, "seconds": 0.0}
        return {"commands": {k: dict(v) for k, v in self.calls.items()}, "queries": dict(queries)}

    def close(self):
        if self._game is not None:
            self._game.close()
            self._game = None


class InProcessBridge:
    """Drop-in replacement for ``supervisor.CLIBridge`` without child processes."""

    def __init__(self, runtime_root, *, session=None, on_call=None):
        self.runtime = Path(runtime_root).resolve()
        self.session = session or Session(self.runtime)
        self.on_call = on_call

    def invoke(self, args, *, stop_requested=None, on_stop=None):
        from ..supervisor import CLIError

        args = [str(a) for a in args]
        started = time.perf_counter()
        outcome: dict = {}

        def work():
            try:
                outcome["result"] = self.session.run(args)
            except TYPED_ERRORS as exc:
                outcome["error"] = CLIError(str(exc))
            except (Exception, SystemExit):  # reported to the supervisor as a failed receipt
                outcome["error"] = CLIError("invalid typed CLI receipt: " + traceback.format_exc()[-800:])

        if stop_requested is None:
            work()
        else:
            # Same contract as the child-process bridge: keep watching for a stop
            # request while the slice runs, signal it once, then let the slice
            # finish at its own safe boundary.
            thread = threading.Thread(target=work, name="factorio-slice", daemon=True)
            thread.start()
            stopped = False
            while thread.is_alive():
                if not stopped and stop_requested():
                    stopped = True
                    if on_stop:
                        try:
                            on_stop()
                        except Exception:
                            # Never abandon a running slice; it stops at its own boundary.
                            pass
                thread.join(0.25)
        error = outcome.get("error")
        result = outcome.get("result")
        if error is None and (not isinstance(result, dict) or result.get("status") == "error"):
            error = CLIError(
                str(result.get("reason", "typed CLI failure")) if isinstance(result, dict) else "invalid CLI object"
            )
        if self.on_call:
            self.on_call(args[0], time.perf_counter() - started, None if error is None else str(error))
        if error is not None:
            raise error
        return result

    def validate_requests(self, rows):
        if not rows:
            return
        from ..supervisor import CLIError

        try:
            if not isinstance(rows, list) or len(rows) > 64:
                raise ValueError("bounded typed request batch required")
            for row in rows:
                Request(**row).validate()
        except (ValueError, TypeError, KeyError) as exc:
            raise CLIError("typed request batch rejected: " + str(exc)) from exc
