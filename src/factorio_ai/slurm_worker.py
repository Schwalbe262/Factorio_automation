from __future__ import annotations

from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib import request, error


DEFAULT_POLL_SECONDS = 1.0
PLANNER_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "selected_goal": {"type": "string"},
        "action_hint": {"type": ["object", "null"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "safety_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["selected_goal", "action_hint", "confidence", "reason", "safety_notes"],
}


def run_worker(root: Path, poll_seconds: float = DEFAULT_POLL_SECONDS, once: bool = False) -> None:
    for folder in ("queue", "running", "results", "failed", "logs"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    while True:
        write_status(root, "idle")
        task_path = next_task(root)
        if task_path is None:
            if once:
                return
            time.sleep(poll_seconds)
            continue
        run_one(root, task_path)
        if once:
            return


def next_task(root: Path) -> Path | None:
    candidates = sorted((root / "queue").glob("*.json"))
    if not candidates:
        return None
    source = candidates[0]
    target = root / "running" / source.name
    try:
        source.replace(target)
    except FileNotFoundError:
        return None
    return target


def run_one(root: Path, task_path: Path) -> None:
    write_status(root, f"running={task_path.name}")
    progress_path = task_path.with_name(f"{task_path.name}.progress")
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        task = json.loads(task_path.read_text(encoding="utf-8"))
        atomic_json(progress_path, {"running": True, "id": task.get("id"), "started_at": started_at})
        result = execute_task(task)
        result.setdefault("id", task.get("id"))
        result.setdefault("type", task.get("type"))
        result.setdefault("ok", True)
        result["started_at"] = started_at
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(root / "results" / task_path.name, result)
    except Exception as exc:  # noqa: BLE001
        failed = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_json(root / "failed" / task_path.name, failed)
    finally:
        try:
            task_path.unlink()
        except FileNotFoundError:
            pass
        try:
            progress_path.unlink()
        except FileNotFoundError:
            pass


def run_task_file(task_path: Path, result_path: Path) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        task = json.loads(task_path.read_text(encoding="utf-8"))
        result = execute_task(task)
        result.setdefault("id", task.get("id"))
        result.setdefault("type", task.get("type"))
        result.setdefault("ok", True)
        result["started_at"] = started_at
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "id": None,
            "type": None,
            "error": f"{type(exc).__name__}: {exc}",
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    atomic_json(result_path, result)
    return result


def execute_task(task: dict[str, Any]) -> dict[str, Any]:
    task_type = task.get("type")
    if task_type != "planner_request":
        raise ValueError(f"unsupported task type: {task_type}")
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    return run_planner_request(payload)


def run_planner_request(payload: dict[str, Any]) -> dict[str, Any]:
    llm_result = try_llm_planner(payload)
    if llm_result is not None:
        llm_result["source"] = "llm"
        return llm_result
    result = heuristic_planner(payload)
    result["source"] = "heuristic"
    return result


def try_llm_planner(payload: dict[str, Any]) -> dict[str, Any] | None:
    base_url = os.getenv("FACTORIO_AI_LLM_BASE_URL", "").rstrip("/")
    model = os.getenv("FACTORIO_AI_LLM_MODEL", "")
    if not base_url or not model:
        return None
    prompt = (
        "You are a Factorio planning assistant. Choose one safe high-level goal or action hint. "
        "Return only JSON matching the schema.\n\n"
        f"Payload:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    request_payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return strict JSON only. Do not directly mutate game state."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    if os.getenv("FACTORIO_AI_LLM_GUIDED_JSON", "").lower() in {"1", "true", "yes", "on"}:
        request_payload["guided_json"] = PLANNER_RESPONSE_SCHEMA
    req = request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    api_key = os.getenv("FACTORIO_AI_LLM_API_KEY")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with request.urlopen(req, timeout=float(os.getenv("FACTORIO_AI_LLM_TIMEOUT", "60"))) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, error.URLError, json.JSONDecodeError, TimeoutError):
        return None
    try:
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict):
        return normalize_planner_response(parsed)
    return None


def heuristic_planner(payload: dict[str, Any]) -> dict[str, Any]:
    legal_actions = payload.get("legal_actions") if isinstance(payload.get("legal_actions"), list) else []
    action_hint = legal_actions[0] if legal_actions and isinstance(legal_actions[0], dict) else None
    return {
        "ok": True,
        "selected_goal": str(payload.get("goal") or "produce_iron_plate"),
        "action_hint": action_hint,
        "confidence": 0.35,
        "reason": "LLM unavailable; returned first legal local-planner action hint.",
        "safety_notes": ["Local controller must validate the action before execution."],
    }


def normalize_planner_response(raw: dict[str, Any]) -> dict[str, Any]:
    confidence = raw.get("confidence", 0.0)
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.0
    safety = raw.get("safety_notes")
    if not isinstance(safety, list):
        safety = [str(safety)] if safety else []
    action_hint = raw.get("action_hint")
    if action_hint is not None and not isinstance(action_hint, dict):
        action_hint = None
    return {
        "ok": True,
        "selected_goal": str(raw.get("selected_goal") or "produce_iron_plate"),
        "action_hint": action_hint,
        "confidence": max(0.0, min(1.0, confidence_value)),
        "reason": str(raw.get("reason") or ""),
        "safety_notes": [str(item) for item in safety],
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temp.replace(path)


def write_status(root: Path, message: str) -> None:
    status = {
        "message": message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }
    atomic_json(root / "status.txt", status)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Factorio AI Slurm queue worker.")
    parser.add_argument("--root", default=os.getenv("ROOT") or os.getcwd())
    parser.add_argument("--task", help="Run one task file and write --result, for AUTO worker dispatch.")
    parser.add_argument("--result", help="Result path for --task.")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.task:
        if not args.result:
            raise SystemExit("--result is required with --task")
        result = run_task_file(Path(args.task), Path(args.result))
        if not result.get("ok"):
            raise SystemExit(1)
        return
    run_worker(Path(args.root), poll_seconds=args.poll_seconds, once=args.once)


if __name__ == "__main__":
    main()
