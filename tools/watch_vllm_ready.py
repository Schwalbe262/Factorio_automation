"""Poll the scheduler vLLM service heartbeat until it is ready or fails.

Usage: set the scheduler env vars, then run with PYTHONPATH=src.
"""

from __future__ import annotations

import datetime
import time

from factorio_ai import remote_slurm


def main() -> int:
    deadline = time.time() + 2400  # 40 min (FP8 ~27GB download + load on first run)
    ready = False
    while time.time() < deadline:
        time.sleep(90)
        try:
            status = remote_slurm.vllm_service_status()
        except Exception as exc:  # noqa: BLE001
            print("poll error:", type(exc).__name__, str(exc)[:80], flush=True)
            continue
        heartbeat = status.get("heartbeat") or {}
        stamp = datetime.datetime.utcnow().strftime("%H:%M:%S")
        state = heartbeat.get("state")
        reason = str(heartbeat.get("reason") or "")[:60]
        print(f"[{stamp}] ready={status.get('service_ready')} state={state} reason={reason}", flush=True)
        if status.get("service_ready") or state == "ready":
            ready = True
            print("RESULT: FP8 READY")
            break
        if state == "failed":
            print("RESULT: FP8 FAILED:", heartbeat.get("reason"))
            break
    print(f"WATCH DONE ready={ready}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
