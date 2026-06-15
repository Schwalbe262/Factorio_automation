from __future__ import annotations

import base64
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.request import Request
from urllib.request import urlopen
import uuid
from typing import Any

from .config import REPO_ROOT


DEFAULT_HOST = "172.16.10.37"
DEFAULT_USER = "r1jae262"
DEFAULT_PORT = 22
DEFAULT_KEY_NAME = "r1jae262.pem"
DEFAULT_REMOTE_DIR = "~/factorio-ai-worker"
DEFAULT_JOB_NAME = "factorio-ai-worker"
DEFAULT_CONDA_ENV = "factorio-ai"
DEFAULT_SCHEDULER_URL = "http://100.112.168.31:8000"
DEFAULT_SCHEDULER_ACCOUNT = "r1jae262"
LAYOUT_IMPROVEMENT_TASK_TYPE = "layout_improvement_request"
DEFAULT_LAYOUT_SCHEDULER_GPU_MODELS = ("a6000ada", "a6000")
LLM_ENV_VARS = (
    "FACTORIO_AI_LLM_BASE_URL",
    "FACTORIO_AI_LLM_MODEL",
    "FACTORIO_AI_LLM_API_KEY",
    "FACTORIO_AI_LLM_GUIDED_JSON",
    "FACTORIO_AI_LLM_TIMEOUT",
)
VLLM_ENV_VARS = (
    "FACTORIO_AI_HF_HOME",
    "FACTORIO_AI_VLLM_MODEL",
    "FACTORIO_AI_VLLM_PORT",
    "FACTORIO_AI_VLLM_ARGS",
    "FACTORIO_AI_VLLM_CUDA_VISIBLE_DEVICES",
    "FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER",
    "FACTORIO_AI_VLLM_STARTUP_SECONDS",
)
GPU_ENV_VARS = (
    "CUDA_VISIBLE_DEVICES",
    "SLURM_JOB_GPUS",
    "SLURM_STEP_GPUS",
    "SLURM_GPUS_ON_NODE",
)


class RemoteSlurmError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteSlurmConfig:
    enabled: bool
    ssh_path: str
    scp_path: str
    host: str
    user: str
    port: int
    key_path: str
    remote_dir: str
    job_name: str
    conda_env: str
    partition: str
    cpus_per_task: int
    gpus_per_node: int
    gres: str
    time_limit: str
    setup_timeout_seconds: int
    task_timeout_seconds: int


@dataclass(frozen=True)
class StrategyWorkerSpec:
    label: str
    remote_dir: str
    job_name: str


DEFAULT_STRATEGY_WORKERS = (
    StrategyWorkerSpec("4b", "~/factorio-ai-worker", "factorio-ai-worker"),
    StrategyWorkerSpec("9b", "~/factorio-ai-worker-9b", "factorio-ai-worker-9b"),
    StrategyWorkerSpec("27b", "~/factorio-ai-worker-27b", "factorio-ai-worker-27b"),
)


def layout_learning_request_context() -> dict[str, Any]:
    return {
        "return_learned_skills": True,
        "record_only_confirmed": True,
        "instruction": (
            "While idle, keep testing simulation-only layout variants and return reusable learned_skills "
            "only when candidate, sandbox, or before/after evidence confirms the lesson."
        ),
        "skill_targets": [
            "avoid placing factories on resource patches",
            "keep power grids continuous",
            "leave inserter clearance between coupled assemblers",
            "prefer burner miner to chest for early coal buffering",
            "buffer user-consumed mall outputs into chests",
            "place labs near science-pack production with expansion room",
            "replace burner inserters once regular inserters are available",
            "use direct assembler-to-assembler inserter transfer when machines are within reach",
            "turn belts by setting the corner belt tile to the outgoing segment direction",
            "route offset site links as output-axis, cross-axis, input-axis doglegs so the first and last belts match endpoint inserter roles",
            "orient output inserters to drop away from producer machines and input inserters to drop into consumer machines",
            "connect automated producer/consumer sites with belts after belt automation starts",
        ],
    }


def config() -> RemoteSlurmConfig:
    home_key = str(Path.home() / ".ssh" / DEFAULT_KEY_NAME)
    return RemoteSlurmConfig(
        enabled=_bool_env("FACTORIO_AI_SLURM_ENABLED", False),
        ssh_path=os.getenv("SUPERCOMPUTER_WORKER_SSH_PATH") or os.getenv("LICENSE_SSH_PATH") or "ssh",
        scp_path=os.getenv("SUPERCOMPUTER_WORKER_SCP_PATH") or "scp",
        host=os.getenv("SUPERCOMPUTER_WORKER_SSH_HOST") or os.getenv("LICENSE_SSH_HOST") or DEFAULT_HOST,
        user=os.getenv("SUPERCOMPUTER_WORKER_SSH_USER") or os.getenv("LICENSE_SSH_USER") or DEFAULT_USER,
        port=_int_env("SUPERCOMPUTER_WORKER_SSH_PORT", _int_env("LICENSE_SSH_PORT", DEFAULT_PORT, 1), 1),
        key_path=os.getenv("SUPERCOMPUTER_WORKER_SSH_KEY") or os.getenv("LICENSE_SSH_KEY") or home_key,
        remote_dir=os.getenv("FACTORIO_AI_SLURM_REMOTE_DIR")
        or os.getenv("SUPERCOMPUTER_WORKER_REMOTE_DIR")
        or DEFAULT_REMOTE_DIR,
        job_name=os.getenv("FACTORIO_AI_SLURM_JOB_NAME") or DEFAULT_JOB_NAME,
        conda_env=os.getenv("FACTORIO_AI_SLURM_CONDA_ENV") or DEFAULT_CONDA_ENV,
        partition=os.getenv("FACTORIO_AI_SLURM_PARTITION") or "gpu4,gpu3,gpu2,gpu1,cpu2,cpu1",
        cpus_per_task=_int_env("FACTORIO_AI_SLURM_CPUS_PER_TASK", 8, 1),
        gpus_per_node=min(3, _int_env("FACTORIO_AI_SLURM_GPUS_PER_NODE", 1, 0)),
        gres=os.getenv("FACTORIO_AI_SLURM_GRES")
        or (f"gpu:{min(3, _int_env('FACTORIO_AI_SLURM_GPUS_PER_NODE', 1, 0))}" if _int_env("FACTORIO_AI_SLURM_GPUS_PER_NODE", 1, 0) > 0 else ""),
        time_limit=os.getenv("FACTORIO_AI_SLURM_TIME") or "24:00:00",
        setup_timeout_seconds=_int_env("FACTORIO_AI_SLURM_SETUP_TIMEOUT_SECONDS", 1800, 60),
        task_timeout_seconds=_int_env("FACTORIO_AI_SLURM_TASK_TIMEOUT_SECONDS", 300, 5),
    )


def _use_scheduler_tasks() -> bool:
    mode = os.getenv("FACTORIO_AI_SLURM_MODE", "auto").strip().lower()
    if mode in {"scheduler", "slurm_scheduler", "scheduler_tasks"}:
        return True
    if mode in {"queue", "worker_queue", "attach", "attached", "srun", "auto_srun"}:
        return False
    return _bool_env("FACTORIO_AI_SLURM_SCHEDULER_ENABLED", False)


def _legacy_direct_slurm_allowed() -> bool:
    return _bool_env("FACTORIO_AI_ALLOW_LEGACY_DIRECT_SLURM", False)


def _legacy_direct_slurm_disabled_result(action: str) -> dict[str, Any]:
    return {
        "ok": False,
        "action": "legacy_direct_slurm_disabled",
        "requestedAction": action,
        "schedulerUrl": _scheduler_url(),
        "account": _scheduler_account(),
        "remediation": (
            "Set FACTORIO_AI_SLURM_MODE=scheduler for slurm_scheduler /tasks, "
            "or set FACTORIO_AI_ALLOW_LEGACY_DIRECT_SLURM=1 for an explicit legacy sbatch run."
        ),
    }


def _scheduler_url() -> str:
    return (os.getenv("FACTORIO_AI_SLURM_SCHEDULER_URL") or DEFAULT_SCHEDULER_URL).strip().rstrip("/")


def _scheduler_account() -> str:
    return (os.getenv("FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT") or DEFAULT_SCHEDULER_ACCOUNT).strip()


def _scheduler_required_capability() -> str:
    return os.getenv("FACTORIO_AI_SLURM_SCHEDULER_REQUIRED_CAPABILITY", "").strip()


def _scheduler_remote_cwd(cfg: RemoteSlurmConfig) -> str:
    return os.getenv("FACTORIO_AI_SLURM_SCHEDULER_REMOTE_CWD", "").strip() or f"{cfg.remote_dir}/factorio-ai"


def _scheduler_api_json(path: str, *, timeout: int = 30) -> Any:
    url = urljoin(_scheduler_url() + "/", path.lstrip("/"))
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 - configured trusted scheduler endpoint
        return json.loads(response.read().decode("utf-8"))


def _scheduler_post_form(path: str, data: dict[str, Any], *, timeout: int = 30) -> str:
    url = urljoin(_scheduler_url() + "/", path.lstrip("/"))
    encoded = urlencode({key: str(value) for key, value in data.items()}).encode("utf-8")
    request = Request(url, data=encoded, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured trusted scheduler endpoint
        return response.read().decode("utf-8", errors="replace")


def _worker_env_values(cfg: RemoteSlurmConfig) -> dict[str, str]:
    values = {
        "FACTORIO_AI_SLURM_CONDA_ENV": cfg.conda_env,
    }
    for name in LLM_ENV_VARS + VLLM_ENV_VARS:
        value = os.getenv(name)
        if value is not None:
            values[name] = _normalize_worker_env_value(name, value)
    if values.get("FACTORIO_AI_VLLM_MODEL") and not values.get("FACTORIO_AI_LLM_MODEL"):
        values["FACTORIO_AI_LLM_MODEL"] = values["FACTORIO_AI_VLLM_MODEL"]
    if values.get("FACTORIO_AI_VLLM_MODEL") and not values.get("FACTORIO_AI_LLM_BASE_URL"):
        port = values.get("FACTORIO_AI_VLLM_PORT") or "8000"
        values["FACTORIO_AI_LLM_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
    return values


def _scheduler_env_setup() -> str:
    lines = []
    for name in LLM_ENV_VARS + VLLM_ENV_VARS:
        value = os.getenv(name)
        if value:
            lines.append(f"export {name}={shlex.quote(_normalize_worker_env_value(name, value))}")
    conda_env = os.getenv("FACTORIO_AI_SLURM_CONDA_ENV")
    if conda_env:
        lines.append(f"export FACTORIO_AI_SLURM_CONDA_ENV={shlex.quote(conda_env.strip())}")
    return "\n".join(lines)


def _scheduler_gpu_model_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _split_scheduler_gpu_models(value: str) -> list[str]:
    models: list[str] = []
    for raw in value.replace(";", ",").split(","):
        model = _scheduler_gpu_model_name(raw)
        if model and model not in models:
            models.append(model)
    return models


def _scheduler_gpu_model_candidates(task_type: str | None = None) -> list[str]:
    if task_type == LAYOUT_IMPROVEMENT_TASK_TYPE:
        configured = os.getenv("FACTORIO_AI_SLURM_LAYOUT_GPU_MODELS", "").strip()
        if not configured:
            configured = os.getenv("FACTORIO_AI_SLURM_LAYOUT_GPU_MODEL", "").strip()
        return _split_scheduler_gpu_models(configured) or list(DEFAULT_LAYOUT_SCHEDULER_GPU_MODELS)
    model = _scheduler_gpu_model_name(os.getenv("FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL", "rtx3090"))
    return [model] if model else []


def _select_scheduler_gpu_model(
    candidates: list[str],
    allocation_rows: list[dict[str, Any]],
    capacity_rows: list[dict[str, Any]],
    account: str,
) -> str:
    if not candidates:
        return ""
    ready_free_by_model: dict[str, int] = {}
    for row in allocation_rows:
        if str(row.get("account_name") or "") != account:
            continue
        if int(row.get("total_gpus") or 0) <= 0:
            continue
        if str(row.get("state") or "") not in {"warm", "running", "active"}:
            continue
        model = _scheduler_gpu_model_name(row.get("gpu_model"))
        ready_free_by_model[model] = ready_free_by_model.get(model, 0) + int(row.get("free_gpus") or 0)

    pending_by_model: dict[str, int] = {}
    for row in capacity_rows:
        model = _scheduler_gpu_model_name(row.get("gpu_model"))
        pending_by_model[model] = pending_by_model.get(model, 0) + int(row.get("pending_gpu_tasks") or 0)

    for model in candidates:
        if ready_free_by_model.get(model, 0) > pending_by_model.get(model, 0):
            return model
    for model in candidates:
        if ready_free_by_model.get(model, 0) > 0:
            return model
    return candidates[0]


def _scheduler_selected_gpu_model(task_type: str | None = None) -> str:
    candidates = _scheduler_gpu_model_candidates(task_type)
    if len(candidates) <= 1:
        return candidates[0] if candidates else ""
    try:
        allocations = _scheduler_api_json("/api/allocations", timeout=10)
        gpu_capacity = _scheduler_api_json("/api/gpu-capacity", timeout=10)
    except Exception:  # noqa: BLE001
        return candidates[0]
    allocation_rows = allocations if isinstance(allocations, list) else []
    capacity_rows = gpu_capacity if isinstance(gpu_capacity, list) else []
    return _select_scheduler_gpu_model(candidates, allocation_rows, capacity_rows, _scheduler_account())


def _scheduler_task_resources(
    task_type: str | None = None,
    gpu_model: str | None = None,
) -> dict[str, Any]:
    candidates = _scheduler_gpu_model_candidates(task_type)
    selected_gpu_model = _scheduler_gpu_model_name(gpu_model)
    if not selected_gpu_model:
        selected_gpu_model = candidates[0] if candidates else ""
    return {
        "cpus": _scheduler_task_cpus(task_type),
        "memory_mb": _int_env("FACTORIO_AI_SLURM_SCHEDULER_MEMORY_MB", 32768, 1024),
        "gpus": _int_env("FACTORIO_AI_SLURM_SCHEDULER_GPUS", 1, 0),
        "gpu_model": selected_gpu_model,
        "partition": os.getenv("FACTORIO_AI_SLURM_SCHEDULER_PARTITION", "auto").strip() or "auto",
        "node_name": os.getenv("FACTORIO_AI_SLURM_SCHEDULER_NODE", "").strip(),
        "exclusive_node": os.getenv("FACTORIO_AI_SLURM_SCHEDULER_EXCLUSIVE_NODE", "0").strip().lower()
        in {"1", "true", "yes", "on"},
    }


def _scheduler_task_cpus(task_type: str | None = None) -> int:
    if task_type == LAYOUT_IMPROVEMENT_TASK_TYPE:
        if os.getenv("FACTORIO_AI_SLURM_LAYOUT_CPUS", "").strip():
            return _int_env("FACTORIO_AI_SLURM_LAYOUT_CPUS", 3, 1)
        return _int_env("FACTORIO_AI_SLURM_SCHEDULER_CPUS", 3, 1)
    return _int_env("FACTORIO_AI_SLURM_SCHEDULER_CPUS", 4, 1)


def _scheduler_task_command(task: dict[str, Any]) -> str:
    task_id = str(task["id"])
    task_json = shlex.quote(f".factorio-ai-scheduler-tasks/{task_id}.json")
    result_json = shlex.quote(f".factorio-ai-scheduler-tasks/{task_id}.result.json")
    safe_task_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in task_id)[:96] or "task"
    vllm_log_base = shlex.quote(f"logs/vllm-scheduler-{safe_task_id}")
    startup_seconds = _int_env("FACTORIO_AI_VLLM_STARTUP_SECONDS", 240, 1)
    return f"""set -euo pipefail
mkdir -p .factorio-ai-scheduler-tasks logs
TASK_JSON={task_json}
RESULT_JSON={result_json}
VLLM_LOG_BASE={vllm_log_base}
if [[ ! -s "$TASK_JSON" ]]; then
  python3 - "$TASK_JSON" <<'PY'
import json
import sys
print(json.dumps({{"ok": False, "error": "scheduler task payload file missing", "task_path": sys.argv[1]}}))
PY
  exit 1
fi
rm -f "$RESULT_JSON"
if command -v conda >/dev/null 2>&1; then
  eval "$(conda shell.bash hook)" || true
  conda activate "${{FACTORIO_AI_SLURM_CONDA_ENV:-factorio-ai}}" || true
elif [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
  . "$HOME/miniconda3/etc/profile.d/conda.sh"
  conda activate "${{FACTORIO_AI_SLURM_CONDA_ENV:-factorio-ai}}" || true
fi
export PYTHONPATH="${{PWD}}/src:${{PYTHONPATH:-}}"
if [ -n "${{FACTORIO_AI_VLLM_MODEL:-}}" ] && [ -z "${{FACTORIO_AI_LLM_MODEL:-}}" ]; then
  export FACTORIO_AI_LLM_MODEL="$FACTORIO_AI_VLLM_MODEL"
fi
if [ -n "${{FACTORIO_AI_VLLM_MODEL:-}}" ] && [ -z "${{FACTORIO_AI_LLM_BASE_URL:-}}" ]; then
  export FACTORIO_AI_LLM_BASE_URL="http://127.0.0.1:${{FACTORIO_AI_VLLM_PORT:-8000}}/v1"
fi
if [ -n "${{FACTORIO_AI_HF_HOME:-}}" ]; then
  export HF_HOME="$FACTORIO_AI_HF_HOME"
fi
if [ -n "${{FACTORIO_AI_VLLM_CUDA_VISIBLE_DEVICES:-}}" ]; then
  export CUDA_VISIBLE_DEVICES="$FACTORIO_AI_VLLM_CUDA_VISIBLE_DEVICES"
fi
if [ -n "${{FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER:-}}" ]; then
  export VLLM_USE_FLASHINFER_SAMPLER="$FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER"
fi
if [ -n "${{FACTORIO_AI_VLLM_MODEL:-}}" ]; then
  VLLM_START_ERROR=""
  if ! python - <<'PY' >/dev/null 2>&1
import os, urllib.request
url = os.environ.get("FACTORIO_AI_LLM_BASE_URL", "").rstrip("/") + "/models"
urllib.request.urlopen(url, timeout=5).read()
PY
  then
    if command -v vllm >/dev/null 2>&1; then
      nohup vllm serve "$FACTORIO_AI_VLLM_MODEL" --host 127.0.0.1 --port "${{FACTORIO_AI_VLLM_PORT:-8000}}" ${{FACTORIO_AI_VLLM_ARGS:-}} > "$VLLM_LOG_BASE.out" 2> "$VLLM_LOG_BASE.err" &
    elif python - <<'PY' >/dev/null 2>&1
import importlib.util
raise SystemExit(0 if importlib.util.find_spec("vllm") else 1)
PY
    then
      nohup python -m vllm.entrypoints.openai.api_server --model "$FACTORIO_AI_VLLM_MODEL" --host 127.0.0.1 --port "${{FACTORIO_AI_VLLM_PORT:-8000}}" ${{FACTORIO_AI_VLLM_ARGS:-}} > "$VLLM_LOG_BASE.out" 2> "$VLLM_LOG_BASE.err" &
    else
      VLLM_START_ERROR="vllm command/module not found after conda activation"
      printf '%s\\n' "$VLLM_START_ERROR" > "$VLLM_LOG_BASE.err"
    fi
  fi
  if [ -n "$VLLM_START_ERROR" ]; then
    python - "$RESULT_JSON" "$FACTORIO_AI_LLM_BASE_URL" "$VLLM_LOG_BASE.err" "$VLLM_START_ERROR" <<'PY'
import json
import sys
result_path, base_url, log_path, error = sys.argv[1:5]
payload = {{"ok": False, "error": error, "llm_error": error, "llm_base_url": base_url, "vllm_log": log_path}}
with open(result_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
PY
    cat "$RESULT_JSON"
    exit 1
  fi
  if ! python - <<'PY'
import os, time, urllib.request
deadline = time.time() + {startup_seconds}
url = os.environ.get("FACTORIO_AI_LLM_BASE_URL", "").rstrip("/") + "/models"
while time.time() < deadline:
    try:
        urllib.request.urlopen(url, timeout=5).read()
        raise SystemExit(0)
    except Exception:
        time.sleep(2)
raise SystemExit(1)
PY
  then
    python - "$RESULT_JSON" "$FACTORIO_AI_LLM_BASE_URL" "$VLLM_LOG_BASE.err" <<'PY'
import json
import sys
result_path, base_url, log_path = sys.argv[1:4]
error = "vllm endpoint not ready before FACTORIO_AI_VLLM_STARTUP_SECONDS"
payload = {{"ok": False, "error": error, "llm_error": error, "llm_base_url": base_url, "vllm_log": log_path}}
with open(result_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
PY
    cat "$RESULT_JSON"
    exit 1
  fi
fi
python -m factorio_ai.slurm_worker --task "$TASK_JSON" --result "$RESULT_JSON"
cat "$RESULT_JSON"
"""


def _resolve_scheduler_remote_cwd(cfg: RemoteSlurmConfig) -> str:
    raw_cwd = _scheduler_remote_cwd(cfg)
    output = _run_remote(
        f"""set -euo pipefail
SCHEDULER_CWD_RAW={json.dumps(raw_cwd)}
if [[ "$SCHEDULER_CWD_RAW" == "~" ]]; then
  SCHEDULER_CWD="$HOME"
elif [[ "$SCHEDULER_CWD_RAW" == "~/"* ]]; then
  SCHEDULER_CWD="$HOME/${{SCHEDULER_CWD_RAW:2}}"
elif [[ "$SCHEDULER_CWD_RAW" == /* ]]; then
  SCHEDULER_CWD="$SCHEDULER_CWD_RAW"
else
  SCHEDULER_CWD="$PWD/$SCHEDULER_CWD_RAW"
fi
mkdir -p "$SCHEDULER_CWD/.factorio-ai-scheduler-tasks" "$SCHEDULER_CWD/logs"
printf '%s\\n' "$SCHEDULER_CWD"
""",
        cfg,
        timeout=60,
    )
    return output.splitlines()[-1]


def _upload_scheduler_task_payload(task: dict[str, Any], cfg: RemoteSlurmConfig, remote_cwd: str) -> None:
    task_id = str(task["id"])
    payload_bytes = json.dumps(task, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    remote_task_dir = f"{remote_cwd}/.factorio-ai-scheduler-tasks"
    remote_target = f"{remote_task_dir}/{task_id}.json"
    remote_temp = f"{remote_task_dir}/.{task_id}.{uuid.uuid4().hex}.tmp"
    local_temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="factorio-ai-scheduler-task-", suffix=".json", delete=False) as file:
            file.write(payload_bytes)
            local_temp_path = Path(file.name)
        _run_scp(local_temp_path, f"{cfg.user}@{cfg.host}:{remote_temp}", cfg, timeout=cfg.setup_timeout_seconds)
        _run_remote(
            f"""set -euo pipefail
REMOTE_TEMP={json.dumps(remote_temp)}
REMOTE_TARGET={json.dumps(remote_target)}
if [[ ! -s "$REMOTE_TEMP" ]]; then
  echo "scheduler task payload upload is empty: $REMOTE_TEMP" >&2
  exit 1
fi
mv "$REMOTE_TEMP" "$REMOTE_TARGET"
""",
            cfg,
            timeout=60,
        )
    finally:
        if local_temp_path is not None:
            try:
                local_temp_path.unlink()
            except FileNotFoundError:
                pass


def _submit_scheduler_task(task: dict[str, Any], cfg: RemoteSlurmConfig) -> dict[str, Any]:
    task_type = str(task.get("type") or "")
    selected_gpu_model = _scheduler_selected_gpu_model(task_type)
    resources = _scheduler_task_resources(task_type, gpu_model=selected_gpu_model)
    name = f"factorio-{str(task.get('type') or 'task').replace('_', '-')}-{uuid.uuid4().hex[:8]}"
    remote_cwd = _resolve_scheduler_remote_cwd(cfg)
    _upload_scheduler_task_payload(task, cfg, remote_cwd)
    data = {
        "name": name,
        "remote_cwd": remote_cwd,
        "command": _scheduler_task_command(task),
        "env_setup": _scheduler_env_setup(),
        "required_capability": _scheduler_required_capability(),
        "env_profile": os.getenv("FACTORIO_AI_SLURM_SCHEDULER_ENV_PROFILE", "").strip(),
        "account_name": _scheduler_account(),
        **resources,
    }
    _scheduler_post_form("/tasks", data, timeout=30)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for row in _scheduler_task_rows():
            if str(row.get("name") or "") == name:
                return row
        time.sleep(1)
    raise RemoteSlurmError(f"scheduler accepted task but it did not appear in /api/tasks: {name}")


def _scheduler_task_rows() -> list[dict[str, Any]]:
    rows = _scheduler_api_json("/api/tasks", timeout=30)
    return rows if isinstance(rows, list) else []


def _scheduler_compact_task_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "name",
        "status",
        "account_name",
        "allocation_id",
        "created_at",
        "attached_at",
        "started_at",
        "finished_at",
        "stdout_path",
        "stderr_path",
        "failure_message",
        "gpus",
        "gpu_model",
        "partition",
        "node_name",
        "required_capability",
    ]
    compact = {key: row.get(key) for key in keys if key in row}
    failure = compact.get("failure_message")
    if isinstance(failure, str) and len(failure) > 500:
        compact["failure_message"] = failure[:500] + "...<truncated>"
    return compact


def _scheduler_compact_allocation_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "account_name",
        "partition",
        "node_name",
        "slurm_job_id",
        "state",
        "total_cpus",
        "free_cpus",
        "total_memory_mb",
        "free_memory_mb",
        "total_gpus",
        "free_gpus",
        "gpu_model",
        "resource_pool",
        "pending_reason",
        "created_at",
        "submitted_at",
        "started_at",
        "closed_at",
        "stdout_path",
        "stderr_path",
        "failure_message",
    ]
    compact = {key: row.get(key) for key in keys if key in row}
    failure = compact.get("failure_message")
    if isinstance(failure, str) and len(failure) > 500:
        compact["failure_message"] = failure[:500] + "...<truncated>"
    return compact


def _read_scheduler_task_stdout(row: dict[str, Any], cfg: RemoteSlurmConfig) -> str:
    path = str(row.get("stdout_path") or "")
    if not path:
        return ""
    return _run_remote(
        f"""set -euo pipefail
PATH_VALUE={json.dumps(path)}
if [[ "$PATH_VALUE" == /* ]]; then
  cat "$PATH_VALUE"
else
  cat "$HOME/$PATH_VALUE"
fi
""",
        cfg,
        timeout=30,
    )


def _parse_last_json_object(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _request_task_via_scheduler(task: dict[str, Any], cfg: RemoteSlurmConfig, timeout_seconds: int | None = None) -> dict[str, Any]:
    row = _submit_scheduler_task(task, cfg)
    task_id = row.get("id")
    deadline = time.monotonic() + (timeout_seconds or max(cfg.task_timeout_seconds, 120))
    expected_account = _scheduler_account()
    while time.monotonic() < deadline:
        rows = _scheduler_task_rows()
        current = next((item for item in rows if item.get("id") == task_id), row)
        account = str(current.get("account_name") or "")
        if account and expected_account and account != expected_account:
            raise RemoteSlurmError(f"scheduler task attached to unexpected account {account}; expected {expected_account}")
        status = str(current.get("status") or "")
        if status == "completed":
            stdout = _read_scheduler_task_stdout(current, cfg)
            parsed = _parse_last_json_object(stdout)
            if parsed is None:
                raise RemoteSlurmError(f"scheduler task completed without JSON result: {stdout[:500]}")
            if not parsed.get("ok"):
                raise RemoteSlurmError(f"scheduler task failed: {parsed}")
            return parsed
        if status in {"failed", "cancelled"}:
            stdout = _read_scheduler_task_stdout(current, cfg)
            raise RemoteSlurmError(f"scheduler task {status}: {current.get('failure_message') or stdout[:500]}")
        time.sleep(2)
    raise TimeoutError(f"scheduler task timed out: {task_id}")


def _normalize_worker_env_value(name: str, value: str) -> str:
    value = value.strip()
    if name == "FACTORIO_AI_LLM_BASE_URL":
        return "".join(value.split())
    return value


def deploy(cfg: RemoteSlurmConfig | None = None) -> dict[str, Any]:
    cfg = cfg or config()
    remote_dir = resolve_remote_dir(cfg)
    worker_env = base64.b64encode(json.dumps(_worker_env_values(cfg), separators=(",", ":")).encode("utf-8")).decode(
        "ascii"
    )
    with tempfile.TemporaryDirectory(prefix="factorio-ai-deploy-") as temp_dir:
        archive_path = Path(temp_dir) / "factorio-ai.tar.gz"
        _create_archive(archive_path)
        remote_archive = f"{remote_dir}/factorio-ai.tar.gz"
        _run_scp(archive_path, f"{cfg.user}@{cfg.host}:{remote_archive}", cfg, timeout=cfg.setup_timeout_seconds)

    output = _run_remote(
        f"""set -euo pipefail
REMOTE_DIR={json.dumps(remote_dir)}
ENV_NAME={json.dumps(cfg.conda_env)}
WORKER_ENV={json.dumps(worker_env)}
mkdir -p "$REMOTE_DIR"/{{queue,running,results,failed,logs}}
rm -rf "$REMOTE_DIR/factorio-ai"
tar -xzf "$REMOTE_DIR/factorio-ai.tar.gz" -C "$REMOTE_DIR"
python3 - "$REMOTE_DIR/config.env" "$WORKER_ENV" <<'PY'
import base64
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
env = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
existing = {{}}
if path.exists():
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith("FACTORIO_AI_"):
            existing[key] = value
existing.update(env)
lines = []
for key in sorted(existing):
    value = str(existing[key]).replace(chr(10), " ")
    lines.append(key + "=" + value)
path.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
PY
cd "$REMOTE_DIR/factorio-ai"
if command -v conda >/dev/null 2>&1; then
  eval "$(conda shell.bash hook)"
  if ! conda env list | awk '{{print $1}}' | grep -qx "$ENV_NAME"; then
    conda create -y -n "$ENV_NAME" python=3.10
  fi
  conda activate "$ENV_NAME"
fi
python -m pip install --upgrade pip
python -m pip install -e .
python -m compileall -q src
date -Is > "$REMOTE_DIR/.setup-complete"
printf '%s\\n' "$REMOTE_DIR"
""",
        cfg,
        timeout=cfg.setup_timeout_seconds,
    )
    return {"ok": True, "remoteDir": remote_dir, "output": output}


def _worker_submit_command(
    cfg: RemoteSlurmConfig,
    remote_dir: str,
    *,
    dependency_job_id: str | None = None,
) -> str:
    dependency_arg = (
        f'  --dependency=afterany:{shlex.quote(str(dependency_job_id))} \\\n'
        if dependency_job_id
        else ""
    )
    return f"""job_id="$(sbatch --parsable \\
  --job-name="$JOB_NAME" \\
  --nodes=1 \\
  --ntasks=1 \\
  --cpus-per-task="$CPUS" \\
  $([[ -n "$GRES" ]] && printf -- '--gres=%s ' "$GRES") \\
{dependency_arg}  --time="$TIME_LIMIT" \\
  --partition="$PARTITION" \\
  --output="$REMOTE_DIR/logs/%x-%j.out" \\
  --error="$REMOTE_DIR/logs/%x-%j.err" \\
  --export=ALL,ROOT="$REMOTE_DIR",FACTORIO_AI_SLURM_CONDA_ENV="$ENV_NAME" \\
  "$REMOTE_DIR/factorio-ai/slurm/run-factorio-ai-worker.sh")"
echo "submitted_job_id=$job_id"
"""


def submit_worker_job(cfg: RemoteSlurmConfig | None = None) -> dict[str, Any]:
    cfg = cfg or config()
    if _use_scheduler_tasks():
        return {
            "ok": True,
            "schedulerUrl": _scheduler_url(),
            "account": _scheduler_account(),
            "action": "scheduler_managed_no_direct_worker",
            "output": "scheduler mode uses /tasks and does not submit a factorio-ai-worker Slurm job",
        }
    if not _legacy_direct_slurm_allowed():
        return _legacy_direct_slurm_disabled_result("slurm-start-worker")
    remote_dir = resolve_remote_dir(cfg)
    deploy(cfg)
    submit_command = _worker_submit_command(cfg, remote_dir)
    output = _run_remote(
        f"""set -euo pipefail
REMOTE_DIR={json.dumps(remote_dir)}
JOB_NAME={json.dumps(cfg.job_name)}
ENV_NAME={json.dumps(cfg.conda_env)}
PARTITION={json.dumps(cfg.partition)}
CPUS={cfg.cpus_per_task}
GPUS={cfg.gpus_per_node}
GRES={json.dumps(cfg.gres)}
TIME_LIMIT={json.dumps(cfg.time_limit)}
mkdir -p "$REMOTE_DIR"/logs
if squeue -h -u "$USER" -n "$JOB_NAME" -t R,PD | awk 'NF{{found=1}} END{{exit !found}}'; then
  echo "already_running=1"
  squeue -h -u "$USER" -n "$JOB_NAME" -t R,PD -o "%i|%T|%M|%L|%R"
  exit 0
fi
{submit_command}
""",
        cfg,
        timeout=60,
    )
    return {"ok": True, "remoteDir": remote_dir, "output": output}


def ensure_worker_job(
    cfg: RemoteSlurmConfig | None = None,
    *,
    renew_before_minutes: int | None = None,
) -> dict[str, Any]:
    cfg = cfg or config()
    if _use_scheduler_tasks():
        return {
            "ok": True,
            "schedulerUrl": _scheduler_url(),
            "account": _scheduler_account(),
            "action": "scheduler_managed_no_direct_worker",
            "renewBeforeSeconds": max(60, int(renew_before_minutes or _int_env("FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES", 180, 1)) * 60),
            "status": _scheduler_status_payload(),
        }
    if not _legacy_direct_slurm_allowed():
        return _legacy_direct_slurm_disabled_result("slurm-ensure-worker")
    remote_dir = resolve_remote_dir(cfg)
    threshold_minutes = (
        renew_before_minutes
        if renew_before_minutes is not None
        else _int_env("FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES", 180, 1)
    )
    threshold_seconds = max(60, int(threshold_minutes) * 60)
    jobs_output = _run_remote(
        f"""set -euo pipefail
JOB_NAME={json.dumps(cfg.job_name)}
squeue -h -u "$USER" -n "$JOB_NAME" -t R,PD -o "%i|%T|%M|%L|%R|%b" || true
""",
        cfg,
        timeout=45,
    )
    jobs = _parse_squeue_jobs(jobs_output)
    running_jobs = [job for job in jobs if job.get("state") == "RUNNING"]
    pending_jobs = [job for job in jobs if job.get("state") == "PENDING"]
    if pending_jobs:
        return {
            "ok": True,
            "remoteDir": remote_dir,
            "action": "pending_successor_exists",
            "renewBeforeSeconds": threshold_seconds,
            "jobs": jobs,
        }
    if not running_jobs:
        submitted = submit_worker_job(cfg)
        return {
            "ok": True,
            "remoteDir": remote_dir,
            "action": "submitted_missing_worker",
            "renewBeforeSeconds": threshold_seconds,
            "jobs": jobs,
            "submit": submitted,
        }
    running = min(
        running_jobs,
        key=lambda job: _slurm_time_left_seconds(str(job.get("time_left") or "")) or 10**12,
    )
    time_left_seconds = _slurm_time_left_seconds(str(running.get("time_left") or ""))
    if time_left_seconds is None or time_left_seconds > threshold_seconds:
        return {
            "ok": True,
            "remoteDir": remote_dir,
            "action": "renewal_not_needed",
            "renewBeforeSeconds": threshold_seconds,
            "timeLeftSeconds": time_left_seconds,
            "jobs": jobs,
        }
    deploy(cfg)
    dependency_job_id = str(running.get("id") or "").strip()
    submit_command = _worker_submit_command(cfg, remote_dir, dependency_job_id=dependency_job_id)
    output = _run_remote(
        f"""set -euo pipefail
REMOTE_DIR={json.dumps(remote_dir)}
JOB_NAME={json.dumps(cfg.job_name)}
ENV_NAME={json.dumps(cfg.conda_env)}
PARTITION={json.dumps(cfg.partition)}
CPUS={cfg.cpus_per_task}
GPUS={cfg.gpus_per_node}
GRES={json.dumps(cfg.gres)}
TIME_LIMIT={json.dumps(cfg.time_limit)}
mkdir -p "$REMOTE_DIR"/logs
{submit_command}
""",
        cfg,
        timeout=60,
    )
    return {
        "ok": True,
        "remoteDir": remote_dir,
        "action": "submitted_dependent_successor",
        "renewBeforeSeconds": threshold_seconds,
        "timeLeftSeconds": time_left_seconds,
        "dependencyJobId": dependency_job_id,
        "jobs": jobs,
        "output": output,
    }


def _parse_squeue_jobs(output: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 2 or not parts[0]:
            continue
        rows.append(
            {
                "id": parts[0],
                "state": parts[1],
                "elapsed": parts[2] if len(parts) > 2 else "",
                "time_left": parts[3] if len(parts) > 3 else "",
                "reason": parts[4] if len(parts) > 4 else "",
                "gres": parts[5] if len(parts) > 5 else "",
            }
        )
    return rows


def _slurm_time_left_seconds(value: str) -> int | None:
    text = value.strip()
    if not text or text.upper() in {"UNLIMITED", "NOT_SET", "INVALID"}:
        return None
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
    elif len(numbers) == 2:
        hours = 0
        minutes, seconds = numbers
    elif len(numbers) == 1:
        hours = 0
        minutes = 0
        seconds = numbers[0]
    else:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def status(cfg: RemoteSlurmConfig | None = None) -> dict[str, Any]:
    cfg = cfg or config()
    remote_dir = resolve_remote_dir(cfg)
    output = _run_remote(
        f"""set -euo pipefail
JOB_NAME={json.dumps(cfg.job_name)}
REMOTE_DIR={json.dumps(remote_dir)}
echo "--- jobs ---"
JOBS="$(squeue -h -u "$USER" -n "$JOB_NAME" -t R,PD -o "%i|%T|%M|%L|%R|%b" || true)"
printf '%s\\n' "$JOBS"
echo "--- counts ---"
for d in queue running results failed logs; do
  mkdir -p "$REMOTE_DIR/$d"
  printf '%s=%s\\n' "$d" "$(find "$REMOTE_DIR/$d" -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' ')"
done
if [[ -f "$REMOTE_DIR/status.txt" ]]; then
  STATUS_JOB_ID="$(awk -F= '$1 == "job_id" {{print $2; exit}}' "$REMOTE_DIR/status.txt" 2>/dev/null || true)"
  if [[ -n "$STATUS_JOB_ID" ]] && ! printf '%s\\n' "$JOBS" | awk -F'|' -v job="$STATUS_JOB_ID" '$1 == job {{found=1}} END{{exit !found}}'; then
    echo "--- status_stale ---"
    echo "status_stale=true"
    echo "stale_job_id=$STATUS_JOB_ID"
    echo "stale_reason=job_id_not_in_squeue"
  fi
  echo "--- status ---"
  cat "$REMOTE_DIR/status.txt"
fi
""",
        cfg,
        timeout=45,
    )
    return {"ok": True, "remoteDir": remote_dir, "output": output}


def llm_status(cfg: RemoteSlurmConfig | None = None) -> dict[str, Any]:
    cfg = cfg or config()
    if _use_scheduler_tasks():
        return _scheduler_status_payload()
    remote_dir = resolve_remote_dir(cfg)
    local_env = _llm_env_presence(os.environ)
    probe_code = f"""
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

env_names = {json.dumps(list(LLM_ENV_VARS + VLLM_ENV_VARS))}
gpu_env_names = {json.dumps(list(GPU_ENV_VARS))}
safe_value_names = {json.dumps(["FACTORIO_AI_LLM_BASE_URL", "FACTORIO_AI_LLM_MODEL", "FACTORIO_AI_VLLM_MODEL", "FACTORIO_AI_VLLM_PORT"])}
factorio_ai_path = {json.dumps(remote_dir + "/factorio-ai")}

env = {{name: bool(os.getenv(name)) for name in env_names}}
env_values = {{name: ("".join(os.getenv(name, "").split()) if name == "FACTORIO_AI_LLM_BASE_URL" else os.getenv(name, "").strip()) for name in safe_value_names}}
gpu_env = {{name: os.getenv(name, "") for name in gpu_env_names}}
nvidia_smi = shutil.which("nvidia-smi")
gpu_output = ""
gpu_error = ""
gpu_count = 0
if nvidia_smi:
    try:
        proc = subprocess.run([nvidia_smi, "-L"], text=True, capture_output=True, timeout=10, check=False)
        gpu_output = proc.stdout.strip()
        gpu_error = proc.stderr.strip()
        gpu_count = sum(1 for line in gpu_output.splitlines() if line.strip().startswith("GPU "))
    except Exception as exc:
        gpu_error = f"{{type(exc).__name__}}: {{exc}}"

llm_base_url = "".join(os.getenv("FACTORIO_AI_LLM_BASE_URL", "").split()).rstrip("/")
llm_model = os.getenv("FACTORIO_AI_LLM_MODEL", "").strip()
llm_endpoint = {{
    "configured": bool(llm_base_url),
    "models_ok": False,
    "model_visible": False,
    "error": "",
}}
if llm_base_url:
    try:
        with urllib.request.urlopen(f"{{llm_base_url}}/models", timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        llm_endpoint["models_ok"] = True
        data = body.get("data") if isinstance(body, dict) else []
        if isinstance(data, list):
            llm_endpoint["model_visible"] = any(
                isinstance(item, dict) and item.get("id") == llm_model
                for item in data
            )
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        llm_endpoint["error"] = f"{{type(exc).__name__}}: {{exc}}"

print(json.dumps({{
    "env": env,
    "env_values": env_values,
    "vllm_command": shutil.which("vllm") is not None,
    "factorio_ai_deployed": os.path.isdir(factorio_ai_path),
    "llm_endpoint": llm_endpoint,
    "gpu": {{
        "env": gpu_env,
        "nvidia_smi": bool(nvidia_smi),
        "count": gpu_count,
        "output": gpu_output,
        "error": gpu_error,
    }},
}}, separators=(",", ":")))
"""
    encoded_probe = base64.b64encode(probe_code.encode("utf-8")).decode("ascii")
    probe_runner = f"import base64; exec(base64.b64decode({encoded_probe!r}))"
    inner_command = (
        "set -euo pipefail; "
        f"{_attached_env_setup(remote_dir)}"
        f"python3 -c {shlex.quote(probe_runner)}"
    )
    output = ""
    last_status_error: Exception | None = None
    for attempt in range(2):
        try:
            output = _run_remote(
                f"""set -euo pipefail
JOB_NAME={json.dumps(cfg.job_name)}
INNER_COMMAND={shlex.quote(inner_command)}
JOBS="$(squeue -h -u "$USER" -n "$JOB_NAME" -t R,PD -o "%i|%T|%M|%L|%R|%b|%S" || true)"
JOB_ID="$(printf '%s\\n' "$JOBS" | awk -F'|' '$2 == "RUNNING" {{print $1; exit}}' | tr -d '[:space:]')"
if [[ -z "$JOB_ID" ]]; then
  FACTORIO_AI_SLURM_JOBS="$JOBS" python3 - <<'PY'
import json
import os

jobs = []
for line in os.environ.get("FACTORIO_AI_SLURM_JOBS", "").splitlines():
    parts = line.split("|")
    if len(parts) >= 6:
        jobs.append({{
            "id": parts[0],
            "state": parts[1],
            "elapsed": parts[2],
            "time_left": parts[3],
            "reason": parts[4],
            "gres": parts[5],
            "start_time": parts[6] if len(parts) > 6 else "",
        }})
pending_jobs = [job for job in jobs if job.get("state") == "PENDING"]
print(json.dumps({{
    "job_running": False,
    "job_pending": bool(pending_jobs),
    "jobs": jobs,
    "pending_jobs": pending_jobs,
}}, separators=(",", ":")))
PY
  exit 0
fi
srun --jobid="$JOB_ID" --overlap -N1 -n1 -c1 bash -lc "$INNER_COMMAND" < /dev/null
""",
                cfg,
                timeout=90,
            )
            last_status_error = None
            break
        except Exception as exc:  # noqa: BLE001
            last_status_error = exc
            if attempt == 0:
                time.sleep(2)
                continue
    if last_status_error is not None:
        return {
            "ok": True,
            "remoteDir": remote_dir,
            "local_env": local_env,
            "remote": {"job_running": False, "error": f"{type(last_status_error).__name__}: {last_status_error}"},
            "llm_ready": False,
            "missing": ["running Slurm worker job with LLM env"],
            "remediation": _llm_status_remediation(["running Slurm worker job with LLM env"], cfg, False, None),
        }

    remote_payload: dict[str, Any] = {"job_running": True}
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            remote_payload.update(parsed)
            break
    if remote_payload.get("job_running") is False:
        missing = (
            ["Slurm worker job pending GPU allocation", "GPU allocation"]
            if remote_payload.get("job_pending")
            else ["running Slurm worker job with LLM env"]
        )
        return {
            "ok": True,
            "remoteDir": remote_dir,
            "local_env": local_env,
            "remote": remote_payload,
            "llm_ready": False,
            "missing": missing,
            "remediation": _llm_status_remediation(missing, cfg, False, None),
        }
    remote_env = remote_payload.get("env") if isinstance(remote_payload.get("env"), dict) else {}
    missing = [
        name
        for name in ("FACTORIO_AI_LLM_BASE_URL", "FACTORIO_AI_LLM_MODEL")
        if not remote_env.get(name)
    ]
    env_values = remote_payload.get("env_values") if isinstance(remote_payload.get("env_values"), dict) else {}
    gpu = remote_payload.get("gpu") if isinstance(remote_payload.get("gpu"), dict) else {}
    needs_local_gpu = _status_needs_local_gpu(env_values)
    if needs_local_gpu and not _gpu_allocation_visible(gpu):
        missing.append("GPU allocation")
    if not remote_payload.get("factorio_ai_deployed"):
        missing.append("Factorio AI deployment")
    llm_endpoint = remote_payload.get("llm_endpoint") if isinstance(remote_payload.get("llm_endpoint"), dict) else {}
    if env_values.get("FACTORIO_AI_LLM_BASE_URL") and not llm_endpoint.get("models_ok"):
        missing.append("LLM endpoint")
    return {
        "ok": True,
        "remoteDir": remote_dir,
        "local_env": local_env,
        "remote": remote_payload,
        "llm_ready": not missing,
        "missing": missing,
        "remediation": _llm_status_remediation(missing, cfg, bool(remote_payload.get("vllm_command")), gpu),
    }


def layout_improvement_status(cfg: RemoteSlurmConfig | None = None) -> dict[str, Any]:
    cfg = cfg or config()
    if _use_scheduler_tasks():
        return _scheduler_status_payload(LAYOUT_IMPROVEMENT_TASK_TYPE)
    return llm_status(cfg)


def _scheduler_status_payload(task_type: str | None = None) -> dict[str, Any]:
    local_env = _llm_env_presence(os.environ)
    gpu_model_candidates = _scheduler_gpu_model_candidates(task_type)
    scheduler_url = _scheduler_url()
    account = _scheduler_account()
    try:
        health = _scheduler_api_json("/api/health", timeout=10)
        allocations = _scheduler_api_json("/api/allocations", timeout=10)
        gpu_capacity = _scheduler_api_json("/api/gpu-capacity", timeout=10)
        tasks = _scheduler_api_json("/api/tasks", timeout=10)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": True,
            "provider": "slurm_scheduler",
            "schedulerUrl": scheduler_url,
            "account": account,
            "local_env": local_env,
            "llm_ready": False,
            "missing": ["Slurm scheduler API"],
            "remote": {"error": f"{type(exc).__name__}: {exc}"},
            "remediation": {
                "why": "Factorio cannot submit local LLM work until the configured slurm_scheduler API responds.",
                "scheduler_url": scheduler_url,
            },
        }
    allocation_rows = allocations if isinstance(allocations, list) else []
    capacity_rows = gpu_capacity if isinstance(gpu_capacity, list) else []
    task_rows = tasks if isinstance(tasks, list) else []
    selected_gpu_model = _select_scheduler_gpu_model(
        gpu_model_candidates,
        allocation_rows,
        capacity_rows,
        account,
    )
    resources = _scheduler_task_resources(task_type, selected_gpu_model)
    wanted_gpu_models = {_scheduler_gpu_model_name(model) for model in gpu_model_candidates if model}
    active_gpu_allocations = [
        row
        for row in allocation_rows
        if str(row.get("account_name") or "") == account
        and int(row.get("total_gpus") or 0) > 0
        and str(row.get("state") or "") not in {"closed", "failed", "cancelled"}
        and (not wanted_gpu_models or _scheduler_gpu_model_name(row.get("gpu_model")) in wanted_gpu_models)
    ]
    ready_gpu_allocations = [
        row
        for row in active_gpu_allocations
        if str(row.get("state") or "") in {"warm", "running", "active"}
    ]
    pending_gpu_allocations = [
        row for row in active_gpu_allocations if str(row.get("state") or "") == "pending"
    ]
    scheduler_ready_free_gpus = sum(int(row.get("free_gpus") or 0) for row in ready_gpu_allocations)
    ready_free_by_model: dict[str, int] = {}
    ready_slots_by_model: dict[str, int] = {}
    needed_cpus = max(1, int(resources.get("cpus") or 1))
    needed_memory_mb = max(1, int(resources.get("memory_mb") or 1))
    needed_gpus = max(1, int(resources.get("gpus") or 1))
    for row in ready_gpu_allocations:
        model = _scheduler_gpu_model_name(row.get("gpu_model"))
        free_gpus = int(row.get("free_gpus") or 0)
        free_cpus = int(row.get("free_cpus") or row.get("total_cpus") or (needed_cpus * max(1, free_gpus)))
        free_memory_mb = int(
            row.get("free_memory_mb") or row.get("total_memory_mb") or (needed_memory_mb * max(1, free_gpus))
        )
        ready_free_by_model[model] = ready_free_by_model.get(model, 0) + free_gpus
        slots = min(
            free_gpus // needed_gpus,
            free_cpus // needed_cpus,
            free_memory_mb // needed_memory_mb,
        )
        ready_slots_by_model[model] = ready_slots_by_model.get(model, 0) + max(0, slots)
    scheduler_free_gpus = 0
    scheduler_owned_gpus = 0
    pending_gpu_tasks = 0
    pending_by_model: dict[str, int] = {}
    for row in capacity_rows:
        model = _scheduler_gpu_model_name(row.get("gpu_model"))
        if wanted_gpu_models and model not in wanted_gpu_models:
            continue
        pending_tasks = int(row.get("pending_gpu_tasks") or 0)
        scheduler_free_gpus += int(row.get("scheduler_free_gpus") or 0)
        scheduler_owned_gpus += int(row.get("scheduler_owned_gpus") or 0)
        pending_gpu_tasks += pending_tasks
        pending_by_model[model] = pending_by_model.get(model, 0) + pending_tasks
    has_llm_runtime = bool(
        os.getenv("FACTORIO_AI_LLM_BASE_URL", "").strip()
        or os.getenv("FACTORIO_AI_VLLM_MODEL", "").strip()
    )
    needs_gpu = int(resources.get("gpus") or 0) > 0 and not os.getenv("FACTORIO_AI_LLM_BASE_URL", "").strip()
    resource_fit_pending_by_model = _scheduler_resource_fit_pending_gpu_tasks_by_model(
        task_rows,
        wanted_gpu_models,
        account,
        needed_cpus=needed_cpus,
        needed_memory_mb=needed_memory_mb,
        needed_gpus=needed_gpus,
    )
    resource_fit_pending_gpu_tasks = sum(resource_fit_pending_by_model.values())
    active_layout_tasks = (
        _scheduler_active_layout_task_count(task_rows, wanted_gpu_models, account)
        if task_type == LAYOUT_IMPROVEMENT_TASK_TYPE
        else 0
    )
    if wanted_gpu_models:
        has_gpu_queue_capacity = any(
            ready_slots_by_model.get(model, 0) > resource_fit_pending_by_model.get(model, 0)
            for model in wanted_gpu_models
        )
    else:
        has_gpu_queue_capacity = sum(ready_slots_by_model.values()) > resource_fit_pending_gpu_tasks
    has_gpu_path = not needs_gpu or has_gpu_queue_capacity
    missing = []
    if not has_llm_runtime:
        missing.append("FACTORIO_AI_VLLM_MODEL or FACTORIO_AI_LLM_BASE_URL")
    if active_layout_tasks > 0:
        missing.append("active scheduler layout task")
    elif not has_gpu_path:
        if scheduler_ready_free_gpus <= 0:
            missing.append("ready scheduler GPU allocation")
        else:
            missing.append("scheduler GPU queue capacity")
    return {
        "ok": True,
        "provider": "slurm_scheduler",
        "schedulerUrl": scheduler_url,
        "account": account,
        "local_env": local_env,
        "llm_ready": not missing,
        "missing": missing,
        "remote": {
            "health": health,
            "resources": resources,
            "gpu_model_candidates": gpu_model_candidates,
            "selected_gpu_model": selected_gpu_model,
            "active_gpu_allocations": [
                _scheduler_compact_allocation_row(row) for row in active_gpu_allocations
            ],
            "ready_gpu_allocations": [
                _scheduler_compact_allocation_row(row) for row in ready_gpu_allocations
            ],
            "pending_gpu_allocations": [
                _scheduler_compact_allocation_row(row) for row in pending_gpu_allocations
            ],
            "scheduler_owned_gpus": scheduler_owned_gpus,
            "scheduler_free_gpus": scheduler_free_gpus,
            "scheduler_ready_free_gpus": scheduler_ready_free_gpus,
            "scheduler_ready_gpu_slots": sum(ready_slots_by_model.values()),
            "scheduler_gpu_queue_capacity": has_gpu_queue_capacity,
            "pending_gpu_tasks": pending_gpu_tasks,
            "resource_fit_pending_gpu_tasks": resource_fit_pending_gpu_tasks,
            "active_layout_tasks": active_layout_tasks,
            "recent_tasks": [_scheduler_compact_task_row(row) for row in task_rows[:5]],
        },
        "remediation": None
        if not missing
        else {
            "why": "Scheduler mode submits local LLM work through slurm_scheduler /tasks; no factorio-ai-worker job is submitted.",
            "required_local_env": ["FACTORIO_AI_SLURM_MODE=scheduler", "FACTORIO_AI_VLLM_MODEL or FACTORIO_AI_LLM_BASE_URL"],
            "scheduler_url": scheduler_url,
            "account": account,
            "gpu_model": selected_gpu_model,
            "gpu_model_candidates": gpu_model_candidates,
        },
    }


def _scheduler_active_layout_task_count(
    task_rows: list[dict[str, Any]],
    wanted_gpu_models: set[str],
    account: str,
) -> int:
    active_states = {"attaching", "running", "starting"}
    count = 0
    for row in task_rows:
        name = str(row.get("name") or "")
        if not name.startswith("factorio-layout-improvement-request"):
            continue
        row_account = str(row.get("account_name") or account)
        if row_account != account:
            continue
        if str(row.get("status") or "").lower() not in active_states:
            continue
        model = _scheduler_gpu_model_name(row.get("gpu_model"))
        if wanted_gpu_models and model and model not in wanted_gpu_models:
            continue
        if int(row.get("gpus") or 0) <= 0:
            continue
        count += 1
    return count


def _scheduler_resource_fit_pending_gpu_tasks_by_model(
    task_rows: list[dict[str, Any]],
    wanted_gpu_models: set[str],
    account: str,
    *,
    needed_cpus: int,
    needed_memory_mb: int,
    needed_gpus: int,
) -> dict[str, int]:
    pending_by_model: dict[str, int] = {}
    pending_states = {"queued", "pending"}
    for row in task_rows:
        if str(row.get("account_name") or "") != account:
            continue
        if str(row.get("status") or "") not in pending_states:
            continue
        model = _scheduler_gpu_model_name(row.get("gpu_model"))
        if wanted_gpu_models and model not in wanted_gpu_models:
            continue
        if int(row.get("gpus") or 0) <= 0:
            continue
        if int(row.get("gpus") or 0) > needed_gpus:
            continue
        if int(row.get("cpus") or 0) > needed_cpus:
            continue
        if int(row.get("memory_mb") or 0) > needed_memory_mb:
            continue
        pending_by_model[model] = pending_by_model.get(model, 0) + 1
    return pending_by_model


def _scheduler_not_ready_result(status_payload: dict[str, Any]) -> dict[str, Any]:
    missing = [str(item) for item in status_payload.get("missing") or ["LLM not ready"]]
    remote = status_payload.get("remote") if isinstance(status_payload.get("remote"), dict) else {}
    return {
        "ok": False,
        "source": "slurm_scheduler",
        "llm_ready": False,
        "missing": missing,
        "error": "scheduler LLM not ready: " + "; ".join(missing),
        "remote": {
            "scheduler_ready_free_gpus": remote.get("scheduler_ready_free_gpus"),
            "pending_gpu_tasks": remote.get("pending_gpu_tasks"),
            "pending_gpu_allocations": remote.get("pending_gpu_allocations") or [],
        },
    }


def _llm_status_remediation(
    missing: list[str],
    cfg: RemoteSlurmConfig,
    vllm_available: bool,
    gpu: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not missing:
        return None
    gpu = gpu or {}
    if "Slurm worker job pending GPU allocation" in missing:
        why = (
            "Slurm worker job is submitted, but Slurm has not allocated the requested GPUs yet. "
            "The local LLM endpoint will only be available after that pending job starts."
        )
    else:
        why = (
            "Slurm worker allocation exists, but strategy requests cannot use a local LLM until "
            "the LLM endpoint variables, Factorio AI code, and GPU allocation are visible inside the attached job."
        )
    return {
        "why": why,
        "required_remote_env": ["FACTORIO_AI_LLM_BASE_URL", "FACTORIO_AI_LLM_MODEL"],
        "optional_remote_env": ["FACTORIO_AI_LLM_API_KEY", "FACTORIO_AI_LLM_GUIDED_JSON", "FACTORIO_AI_LLM_TIMEOUT"],
        "required_gpu_allocation": {
            "needed": "GPU allocation" in missing or "Slurm worker job pending GPU allocation" in missing,
            "current": gpu,
            "factorio_worker_env": [
                "FACTORIO_AI_SLURM_GPUS_PER_NODE=1",
                "FACTORIO_AI_SLURM_GRES=gpu:1",
            ],
            "legacy_kakao_auto_env": [
                "SUPERCOMPUTER_WORKER_GPUS_PER_NODE=1",
                "SUPERCOMPUTER_WORKER_GRES=gpu:1",
            ],
            "sbatch_option": f"--gres={cfg.gres or 'gpu:1'}",
        },
        "required_deployment": {
            "needed": "Factorio AI deployment" in missing,
            "command": "factorio-ai slurm-deploy",
        },
        "vllm_available_in_job": vllm_available,
        "example_existing_server": [
            "export FACTORIO_AI_LLM_BASE_URL=http://127.0.0.1:8000/v1",
            "export FACTORIO_AI_LLM_MODEL=<openai-compatible-model-name>",
        ],
        "example_vllm_worker": [
            "export FACTORIO_AI_SLURM_GPUS_PER_NODE=1",
            "export FACTORIO_AI_VLLM_MODEL=<huggingface-or-local-model>",
            "factorio-ai slurm-start-worker",
        ],
        "example_auto_reopen": [
            "export FACTORIO_AI_SLURM_GPUS_PER_NODE=1",
            "export FACTORIO_AI_SLURM_GRES=gpu:1",
            "factorio-ai slurm-start-worker",
        ],
        "remote_dir": cfg.remote_dir,
        "job_name": cfg.job_name,
    }


def _status_needs_local_gpu(env_values: dict[str, Any]) -> bool:
    if env_values.get("FACTORIO_AI_VLLM_MODEL"):
        return True
    base_url = "".join(str(env_values.get("FACTORIO_AI_LLM_BASE_URL") or "").split()).lower()
    if not base_url:
        return True
    return "127.0.0.1" in base_url or "localhost" in base_url


def _gpu_allocation_visible(gpu: dict[str, Any]) -> bool:
    try:
        count = int(gpu.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    if count > 0:
        return True
    env = gpu.get("env") if isinstance(gpu.get("env"), dict) else {}
    for name in ("SLURM_JOB_GPUS", "SLURM_STEP_GPUS", "SLURM_GPUS_ON_NODE"):
        if str(env.get(name) or "").strip():
            return True
    cuda_visible = str(env.get("CUDA_VISIBLE_DEVICES") or "").strip().lower()
    return bool(cuda_visible and cuda_visible not in {"none", "no_dev_files", "-1"})


def cancel(cfg: RemoteSlurmConfig | None = None) -> dict[str, Any]:
    cfg = cfg or config()
    output = _run_remote(
        f"""set -euo pipefail
JOB_NAME={json.dumps(cfg.job_name)}
if squeue -h -u "$USER" -n "$JOB_NAME" | awk 'NF{{found=1}} END{{exit !found}}'; then
  scancel -u "$USER" -n "$JOB_NAME"
  echo "cancelled=$JOB_NAME"
else
  echo "missing=$JOB_NAME"
fi
""",
        cfg,
        timeout=45,
    )
    return {"ok": True, "output": output}


def submit_task(task: dict[str, Any], cfg: RemoteSlurmConfig | None = None) -> str:
    cfg = cfg or config()
    remote_dir = resolve_remote_dir(cfg)
    task_id = str(task.get("id") or f"task-{uuid.uuid4().hex}")
    task["id"] = task_id
    task_name = f"{task_id}.json"
    payload_bytes = json.dumps(task, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload_bytes) > 60_000:
        remote_temp = f"{remote_dir}/queue/.{task_name}.{uuid.uuid4().hex}.tmp"
        remote_target = f"{remote_dir}/queue/{task_name}"
        _run_remote(
            f"""set -euo pipefail
REMOTE_DIR={json.dumps(remote_dir)}
TASK_NAME={json.dumps(task_name)}
mkdir -p "$REMOTE_DIR"/{{queue,running,results,failed,logs}}
rm -f "$REMOTE_DIR/results/$TASK_NAME" "$REMOTE_DIR/failed/$TASK_NAME" "$REMOTE_DIR/running/$TASK_NAME" "$REMOTE_DIR/running/$TASK_NAME.progress" "$REMOTE_DIR/queue/$TASK_NAME"
""",
            cfg,
            timeout=60,
        )
        local_temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="factorio-ai-task-", suffix=".json", delete=False) as file:
                file.write(payload_bytes)
                local_temp_path = Path(file.name)
            _run_scp(local_temp_path, f"{cfg.user}@{cfg.host}:{remote_temp}", cfg, timeout=cfg.setup_timeout_seconds)
            _run_remote(
                f"""set -euo pipefail
REMOTE_TEMP={json.dumps(remote_temp)}
REMOTE_TARGET={json.dumps(remote_target)}
mv "$REMOTE_TEMP" "$REMOTE_TARGET"
""",
                cfg,
                timeout=60,
            )
        finally:
            if local_temp_path is not None:
                try:
                    local_temp_path.unlink()
                except FileNotFoundError:
                    pass
        return task_name
    payload = base64.b64encode(payload_bytes.rstrip(b"\n")).decode("ascii")
    _run_remote(
        f"""set -euo pipefail
REMOTE_DIR={json.dumps(remote_dir)}
TASK_NAME={json.dumps(task_name)}
PAYLOAD={json.dumps(payload)}
mkdir -p "$REMOTE_DIR"/{{queue,running,results,failed,logs}}
rm -f "$REMOTE_DIR/results/$TASK_NAME" "$REMOTE_DIR/failed/$TASK_NAME" "$REMOTE_DIR/running/$TASK_NAME"
python3 - "$REMOTE_DIR" "$TASK_NAME" "$PAYLOAD" <<'PY'
import base64
import sys
from pathlib import Path

remote_dir = Path(sys.argv[1])
task_name = sys.argv[2]
payload = base64.b64decode(sys.argv[3])
target = remote_dir / "queue" / task_name
temp = remote_dir / "queue" / f".{task_name}.tmp"
temp.write_bytes(payload + b"\\n")
temp.replace(target)
PY
""",
        cfg,
        timeout=60,
    )
    return task_name


def read_task_state(task_name: str, cfg: RemoteSlurmConfig | None = None) -> tuple[str, dict[str, Any] | None, str]:
    cfg = cfg or config()
    remote_dir = resolve_remote_dir(cfg)
    output = _run_remote(
        f"""set -euo pipefail
REMOTE_DIR={json.dumps(remote_dir)}
TASK_NAME={json.dumps(task_name)}
if [[ -f "$REMOTE_DIR/results/$TASK_NAME" ]]; then
  echo "__STATE__:result"
  cat "$REMOTE_DIR/results/$TASK_NAME"
elif [[ -f "$REMOTE_DIR/failed/$TASK_NAME" ]]; then
  echo "__STATE__:failed"
  cat "$REMOTE_DIR/failed/$TASK_NAME"
elif [[ -f "$REMOTE_DIR/running/$TASK_NAME.progress" ]]; then
  echo "__STATE__:running"
  cat "$REMOTE_DIR/running/$TASK_NAME.progress"
elif [[ -f "$REMOTE_DIR/running/$TASK_NAME" ]]; then
  echo "__STATE__:running"
elif [[ -f "$REMOTE_DIR/queue/$TASK_NAME" ]]; then
  echo "__STATE__:queued"
else
  echo "__STATE__:missing"
fi
""",
        cfg,
        timeout=45,
    )
    lines = output.splitlines()
    if not lines or not lines[0].startswith("__STATE__:"):
        return "unknown", None, output
    state = lines[0].split(":", 1)[1]
    body = "\n".join(lines[1:]).strip()
    if body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return state, None, body
        if isinstance(parsed, dict):
            return state, parsed, body
    return state, None, body


def request_plan(
    observation: dict[str, Any],
    legal_actions: list[dict[str, Any]],
    goal: str,
    cfg: RemoteSlurmConfig | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    cfg = cfg or config()
    task = {
        "id": f"planner-{uuid.uuid4().hex}",
        "type": "planner_request",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "goal": goal,
            "observation": observation,
            "legal_actions": legal_actions,
        },
    }
    if _use_attached_srun(cfg):
        return _request_plan_via_attached_srun(task, cfg, timeout_seconds)
    if _use_scheduler_tasks():
        return _request_task_via_scheduler(task, cfg, timeout_seconds)

    task_name = submit_task(task, cfg)
    deadline = time.monotonic() + (timeout_seconds or cfg.task_timeout_seconds)
    while time.monotonic() < deadline:
        state, data, _raw = read_task_state(task_name, cfg)
        if state == "result" and data is not None:
            return data
        if state == "failed":
            raise RemoteSlurmError(f"remote planner task failed: {data}")
        time.sleep(2)
    raise TimeoutError(f"remote planner task timed out: {task_name}")


def request_strategy(
    objective: str,
    observation: dict[str, Any],
    production_targets: dict[str, float] | None = None,
    selected_improvement_site: dict[str, Any] | None = None,
    available_skills: list[dict[str, Any]] | None = None,
    cfg: RemoteSlurmConfig | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    cfg = cfg or config()
    from .strategy import make_strategy_payload

    targets = production_targets or {}
    task = {
        "id": f"strategy-{uuid.uuid4().hex}",
        "type": "strategy_request",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "objective": objective,
            "observation": observation,
            "production_targets": targets,
            "selected_improvement_site": selected_improvement_site or {},
            "strategy_payload": make_strategy_payload(
                objective,
                observation,
                targets,
                selected_improvement_site=selected_improvement_site,
            ),
            "available_skills": available_skills or [],
        },
    }
    if _use_attached_srun(cfg):
        return _request_task_via_attached_srun(task, cfg, timeout_seconds)
    if _use_scheduler_tasks():
        return _request_task_via_scheduler(task, cfg, timeout_seconds)

    task_name = submit_task(task, cfg)
    deadline = time.monotonic() + (timeout_seconds or cfg.task_timeout_seconds)
    while time.monotonic() < deadline:
        state, data, _raw = read_task_state(task_name, cfg)
        if state == "result" and data is not None:
            return data
        if state == "failed":
            raise RemoteSlurmError(f"remote strategy task failed: {data}")
        time.sleep(2)
    raise TimeoutError(f"remote strategy task timed out: {task_name}")


def parse_strategy_worker_specs(value: str | None) -> list[StrategyWorkerSpec]:
    if not value or not value.strip():
        return list(DEFAULT_STRATEGY_WORKERS)
    specs: list[StrategyWorkerSpec] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        if "=" in item:
            label, remainder = item.split("=", 1)
        else:
            label, remainder = item, item
        if "@" in remainder:
            remote_dir, job_name = remainder.rsplit("@", 1)
        else:
            remote_dir = remainder
            job_name = Path(remote_dir.rstrip("/")).name or DEFAULT_JOB_NAME
        specs.append(
            StrategyWorkerSpec(
                label=label.strip() or job_name.strip() or remote_dir.strip(),
                remote_dir=remote_dir.strip(),
                job_name=job_name.strip() or DEFAULT_JOB_NAME,
            )
        )
    return specs


def compare_strategy_workers(
    *,
    objective: str,
    observation: dict[str, Any],
    production_targets: dict[str, float] | None = None,
    available_skills: list[dict[str, Any]] | None = None,
    workers: list[StrategyWorkerSpec] | None = None,
    cfg: RemoteSlurmConfig | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    base_cfg = cfg or config()
    rows: list[dict[str, Any]] = []
    for spec in workers or list(DEFAULT_STRATEGY_WORKERS):
        worker_cfg = replace(base_cfg, remote_dir=spec.remote_dir, job_name=spec.job_name)
        started = time.monotonic()
        row: dict[str, Any] = {
            "label": spec.label,
            "remote_dir": spec.remote_dir,
            "job_name": spec.job_name,
            "ok": False,
        }
        try:
            status_payload = llm_status(worker_cfg)
            remote = status_payload.get("remote") if isinstance(status_payload.get("remote"), dict) else {}
            env_values = remote.get("env_values") if isinstance(remote.get("env_values"), dict) else {}
            gpu = remote.get("gpu") if isinstance(remote.get("gpu"), dict) else {}
            row["llm_ready"] = bool(status_payload.get("llm_ready"))
            row["model"] = env_values.get("FACTORIO_AI_LLM_MODEL") or env_values.get("FACTORIO_AI_VLLM_MODEL") or ""
            row["gpu_count"] = _safe_int(gpu.get("count"))
            if not status_payload.get("llm_ready"):
                row["error"] = "; ".join(str(item) for item in status_payload.get("missing") or ["LLM not ready"])
            else:
                decision = request_strategy(
                    objective=objective,
                    observation=observation,
                    production_targets=production_targets,
                    available_skills=available_skills,
                    cfg=worker_cfg,
                    timeout_seconds=timeout_seconds,
                )
                row.update(
                    {
                        "ok": True,
                        "source": decision.get("source"),
                        "selected_skill": decision.get("selected_skill") or decision.get("selected_goal"),
                        "priority": decision.get("priority"),
                        "reason": decision.get("reason"),
                        "blockers": decision.get("blockers"),
                        "evidence": decision.get("evidence"),
                        "llm_error": decision.get("llm_error") or decision.get("error") or "",
                        "llm_prompt_chars": decision.get("llm_prompt_chars"),
                        "llm_initial_error": decision.get("llm_initial_error") or "",
                        "llm_initial_prompt_chars": decision.get("llm_initial_prompt_chars"),
                        "llm_retry": decision.get("llm_retry") or "",
                        "quality_warning": decision.get("quality_warning") or "",
                        "llm_max_tokens": decision.get("llm_max_tokens"),
                        "llm_response_snippet": decision.get("llm_response_snippet") or "",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"
        row["latency_ms"] = int((time.monotonic() - started) * 1000)
        rows.append(row)
    return {
        "ok": any(row.get("ok") for row in rows),
        "type": "strategy_worker_comparison",
        "objective": objective,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "workers": rows,
    }


def request_layout_improvement(
    objective: str,
    active_skill: str,
    active_step: int,
    observation: dict[str, Any],
    production_targets: dict[str, float] | None = None,
    factory_monitor: dict[str, Any] | None = None,
    layout_validation_feedback: dict[str, Any] | None = None,
    selected_improvement_site: dict[str, Any] | None = None,
    cfg: RemoteSlurmConfig | None = None,
    timeout_seconds: int | None = None,
    force_attached: bool = False,
) -> dict[str, Any]:
    cfg = cfg or config()
    if _use_scheduler_tasks():
        status_payload = _scheduler_status_payload(LAYOUT_IMPROVEMENT_TASK_TYPE)
        if not status_payload.get("llm_ready"):
            return _scheduler_not_ready_result(status_payload)
    task = {
        "id": f"layout-improvement-{uuid.uuid4().hex}",
        "type": "layout_improvement_request",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "objective": objective,
            "active_skill": active_skill,
            "active_step": active_step,
            "observation": observation,
            "production_targets": production_targets or {},
            "factory_monitor": factory_monitor or {},
            "layout_validation_feedback": layout_validation_feedback or {},
            "selected_improvement_site": selected_improvement_site or {},
            "layout_learning": layout_learning_request_context(),
        },
    }
    if _use_scheduler_tasks():
        return _request_task_via_scheduler(task, cfg, timeout_seconds or max(cfg.task_timeout_seconds, 120))
    if force_attached or _use_attached_srun(cfg):
        return _request_task_via_attached_srun(task, cfg, timeout_seconds or max(cfg.task_timeout_seconds, 120))

    task_name = submit_task(task, cfg)
    deadline = time.monotonic() + (timeout_seconds or cfg.task_timeout_seconds)
    while time.monotonic() < deadline:
        state, data, _raw = read_task_state(task_name, cfg)
        if state == "result" and data is not None:
            return data
        if state == "failed":
            raise RemoteSlurmError(f"remote layout improvement task failed: {data}")
        time.sleep(2)
    raise TimeoutError(f"remote layout improvement task timed out: {task_name}")


def request_strategy_model_benchmark(
    strategy_payload: dict[str, Any],
    models: list[str],
    cfg: RemoteSlurmConfig | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    cfg = cfg or config()
    task = {
        "id": f"strategy-model-benchmark-{uuid.uuid4().hex}",
        "type": "strategy_model_benchmark",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "models": models,
            "strategy_payload": strategy_payload,
        },
    }
    if _use_attached_srun(cfg):
        return _request_task_via_attached_srun(task, cfg, timeout_seconds or max(cfg.task_timeout_seconds, 120))
    if _use_scheduler_tasks():
        return _request_task_via_scheduler(task, cfg, timeout_seconds or max(cfg.task_timeout_seconds, 120))

    task_name = submit_task(task, cfg)
    deadline = time.monotonic() + (timeout_seconds or max(cfg.task_timeout_seconds, 120))
    while time.monotonic() < deadline:
        state, data, _raw = read_task_state(task_name, cfg)
        if state == "result" and data is not None:
            return data
        if state == "failed":
            raise RemoteSlurmError(f"remote strategy model benchmark failed: {data}")
        time.sleep(2)
    raise TimeoutError(f"remote strategy model benchmark timed out: {task_name}")


def _use_attached_srun(cfg: RemoteSlurmConfig) -> bool:
    mode = os.getenv("FACTORIO_AI_SLURM_MODE", "auto").strip().lower()
    if mode in {"queue", "worker_queue"}:
        return False
    if mode in {"attach", "attached", "srun", "auto_srun"}:
        return True
    return cfg.job_name == "AUTO" and cfg.remote_dir.endswith("kakao-bot-worker")


def _request_plan_via_attached_srun(
    task: dict[str, Any],
    cfg: RemoteSlurmConfig,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    return _request_task_via_attached_srun(task, cfg, timeout_seconds)


def _request_task_via_attached_srun(
    task: dict[str, Any],
    cfg: RemoteSlurmConfig,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    remote_dir = resolve_remote_dir(cfg)
    task_name = f"{task['id']}.json"
    result_name = f"{task['id']}.result.json"
    task_path = f"{remote_dir}/{task_name}"
    result_path = f"{remote_dir}/{result_name}"
    remote_temp = f"{remote_dir}/.{task_name}.{uuid.uuid4().hex}.tmp"
    payload_bytes = json.dumps(task, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    local_temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="factorio-ai-attached-task-", suffix=".json", delete=False) as file:
            file.write(payload_bytes)
            local_temp_path = Path(file.name)
        _run_scp(local_temp_path, f"{cfg.user}@{cfg.host}:{remote_temp}", cfg, timeout=cfg.setup_timeout_seconds)
    finally:
        if local_temp_path is not None:
            try:
                local_temp_path.unlink()
            except FileNotFoundError:
                pass
    inner_command = (
        "set -euo pipefail; "
        f"{_attached_env_setup(remote_dir)}"
        f"source ~/miniconda3/etc/profile.d/conda.sh && "
        f"conda activate {shlex.quote(cfg.conda_env)} && "
        f"cd {shlex.quote(remote_dir + '/factorio-ai')} && "
        f"python -m factorio_ai.slurm_worker --task {shlex.quote(task_path)} --result {shlex.quote(result_path)}"
    )
    script = f"""set -euo pipefail
REMOTE_DIR={json.dumps(remote_dir)}
JOB_NAME={json.dumps(cfg.job_name)}
TASK_NAME={json.dumps(task_name)}
RESULT_NAME={json.dumps(result_name)}
REMOTE_TEMP={json.dumps(remote_temp)}
INNER_COMMAND={shlex.quote(inner_command)}
JOB_ID="$(squeue -h -u "$USER" -n "$JOB_NAME" -t R -o "%i" | head -1 | tr -d '[:space:]')"
if [[ -z "$JOB_ID" ]]; then
  echo "__ERROR__:running job not found for $JOB_NAME"
  exit 2
fi
TASK_PATH="$REMOTE_DIR/$TASK_NAME"
RESULT_PATH="$REMOTE_DIR/$RESULT_NAME"
if [[ -f "$RESULT_PATH" ]]; then
  cat "$RESULT_PATH"
  rm -f "$TASK_PATH" "$RESULT_PATH" "$REMOTE_TEMP"
  exit 0
fi
if [[ -f "$REMOTE_TEMP" ]]; then
  mv "$REMOTE_TEMP" "$TASK_PATH"
elif [[ ! -f "$TASK_PATH" ]]; then
  echo "__ERROR__:attached task file missing for retry: $TASK_NAME"
  exit 3
fi
srun --jobid="$JOB_ID" --overlap -N1 -n1 -c1 bash -lc "$INNER_COMMAND" < /dev/null
cat "$RESULT_PATH"
rm -f "$TASK_PATH" "$RESULT_PATH"
"""
    output = _run_remote_attached_task_with_retry(
        script,
        cfg,
        timeout=timeout_seconds or cfg.task_timeout_seconds,
    )
    json_line = ""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            json_line = line
            break
    if not json_line:
        raise RemoteSlurmError(f"attached AUTO task did not return JSON: {output[:500]}")
    result = json.loads(json_line)
    if not isinstance(result, dict):
        raise RemoteSlurmError("attached AUTO task returned non-object JSON")
    if not result.get("ok"):
        raise RemoteSlurmError(f"attached AUTO task failed: {result}")
    return result


def _run_remote_attached_task_with_retry(script: str, cfg: RemoteSlurmConfig, *, timeout: int) -> str:
    attempts = _int_env("FACTORIO_AI_SLURM_ATTACHED_TASK_ATTEMPTS", 2, 1)
    retry_delay = _int_env("FACTORIO_AI_SLURM_ATTACHED_TASK_RETRY_DELAY_SECONDS", 2, 0)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _run_remote(script, cfg, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= attempts or not _attached_task_error_retryable(exc):
                raise
            if retry_delay:
                time.sleep(retry_delay)
    assert last_error is not None
    raise last_error


def _attached_task_error_retryable(exc: Exception) -> bool:
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
        return True
    message = str(exc).lower()
    retryable_markers = (
        "temporarily",
        "timed out",
        "timeout",
        "connection reset",
        "connection closed",
        "connection timed out",
        "kex_exchange_identification",
        "resource temporarily unavailable",
        "step creation temporarily disabled",
        "unable to create step",
        "job step",
    )
    return isinstance(exc, RemoteSlurmError) and any(marker in message for marker in retryable_markers)


def resolve_remote_dir(cfg: RemoteSlurmConfig | None = None) -> str:
    cfg = cfg or config()
    return _run_remote(
        f"""set -euo pipefail
{_remote_dir_script(cfg)}
mkdir -p "$REMOTE_DIR"
printf '%s\\n' "$REMOTE_DIR"
""",
        cfg,
        timeout=30,
    ).splitlines()[-1]


def _bool_env(name: str, fallback: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return fallback
    return value.lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, fallback: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return fallback
    return value if value >= minimum else fallback


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _llm_env_presence(env: Any) -> dict[str, bool]:
    return {name: bool(env.get(name)) for name in LLM_ENV_VARS + VLLM_ENV_VARS}


def _attached_env_setup(remote_dir: str | None = None) -> str:
    commands = []
    if remote_dir:
        config_path = shlex.quote(f"{remote_dir}/config.env")
        commands.append(
            f"if [[ -f {config_path} ]]; then "
            f"while IFS='=' read -r key value; do "
            f"value=\"$(printf '%s' \"$value\" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')\"; "
            f"if [[ \"$key\" == FACTORIO_AI_LLM_BASE_URL ]]; then value=\"$(printf '%s' \"$value\" | tr -d '[:space:]')\"; fi; "
            f"case \"$key\" in FACTORIO_AI_LLM_*|FACTORIO_AI_VLLM_*|FACTORIO_AI_CONDA_ENV) "
            f"export \"$key=$value\";; "
            f"esac; "
            f"done < {config_path}; "
            f"fi"
        )
    for name in LLM_ENV_VARS + VLLM_ENV_VARS:
        value = os.getenv(name)
        if value:
            commands.append(f"export {name}={shlex.quote(_normalize_worker_env_value(name, value))}")
    vllm_model = (os.getenv("FACTORIO_AI_VLLM_MODEL") or "").strip()
    if vllm_model and not os.getenv("FACTORIO_AI_LLM_MODEL"):
        commands.append(f"export FACTORIO_AI_LLM_MODEL={shlex.quote(vllm_model)}")
    if vllm_model and not os.getenv("FACTORIO_AI_LLM_BASE_URL"):
        port = (os.getenv("FACTORIO_AI_VLLM_PORT") or "8000").strip()
        commands.append(f"export FACTORIO_AI_LLM_BASE_URL={shlex.quote(f'http://127.0.0.1:{port}/v1')}")
    return ("; ".join(commands) + "; ") if commands else ""


def _ssh_args(cfg: RemoteSlurmConfig) -> list[str]:
    return [
        cfg.ssh_path,
        "-i",
        cfg.key_path,
        "-p",
        str(cfg.port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{cfg.user}@{cfg.host}",
        "bash -s",
    ]


def _run_remote(script: str, cfg: RemoteSlurmConfig, timeout: int | None = None) -> str:
    proc = subprocess.run(
        _ssh_args(cfg),
        input=script.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"),
        capture_output=True,
        timeout=timeout or 60,
        check=False,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise RemoteSlurmError((stderr or stdout or f"ssh exited with code {proc.returncode}").strip())
    return stdout.strip()


def _run_scp(local_path: Path, remote_target: str, cfg: RemoteSlurmConfig, timeout: int | None = None) -> None:
    proc = subprocess.run(
        [
            cfg.scp_path,
            "-i",
            cfg.key_path,
            "-P",
            str(cfg.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "StrictHostKeyChecking=accept-new",
            str(local_path),
            remote_target,
        ],
        capture_output=True,
        timeout=timeout or 60,
        check=False,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise RemoteSlurmError((stderr or stdout or f"scp exited with code {proc.returncode}").strip())


def _remote_dir_script(cfg: RemoteSlurmConfig) -> str:
    raw = json.dumps(cfg.remote_dir)
    return f"""
REMOTE_DIR_RAW={raw}
if [[ "$REMOTE_DIR_RAW" == "~" ]]; then
  REMOTE_DIR="$HOME"
elif [[ "$REMOTE_DIR_RAW" == "~/"* ]]; then
  REMOTE_DIR="$HOME/${{REMOTE_DIR_RAW:2}}"
elif [[ "$REMOTE_DIR_RAW" == /* ]]; then
  REMOTE_DIR="$REMOTE_DIR_RAW"
else
  REMOTE_DIR="$PWD/$REMOTE_DIR_RAW"
fi
"""


def _create_archive(target: Path) -> None:
    skip_parts = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "runtime", "logs", "factorio_mods"}
    with tarfile.open(target, "w:gz") as archive:
        for path in REPO_ROOT.rglob("*"):
            rel = path.relative_to(REPO_ROOT)
            if any(part in skip_parts for part in rel.parts):
                continue
            if path.is_file():
                archive.add(path, arcname=str(Path("factorio-ai") / rel))
