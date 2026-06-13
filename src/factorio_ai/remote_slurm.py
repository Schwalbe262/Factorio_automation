from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import time
import uuid
from typing import Any

from .config import REPO_ROOT


DEFAULT_HOST = "172.16.10.37"
DEFAULT_USER = "r1jae262"
DEFAULT_PORT = 22
DEFAULT_KEY_NAME = "r1jae262.pem"
DEFAULT_REMOTE_DIR = "~/kakao-bot-worker"
DEFAULT_JOB_NAME = "AUTO"
DEFAULT_CONDA_ENV = "factorio-ai"
LLM_ENV_VARS = (
    "FACTORIO_AI_LLM_BASE_URL",
    "FACTORIO_AI_LLM_MODEL",
    "FACTORIO_AI_LLM_API_KEY",
    "FACTORIO_AI_LLM_GUIDED_JSON",
    "FACTORIO_AI_LLM_TIMEOUT",
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
    time_limit: str
    setup_timeout_seconds: int
    task_timeout_seconds: int


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
        remote_dir=os.getenv("SUPERCOMPUTER_WORKER_REMOTE_DIR") or DEFAULT_REMOTE_DIR,
        job_name=os.getenv("FACTORIO_AI_SLURM_JOB_NAME") or DEFAULT_JOB_NAME,
        conda_env=os.getenv("FACTORIO_AI_SLURM_CONDA_ENV") or DEFAULT_CONDA_ENV,
        partition=os.getenv("FACTORIO_AI_SLURM_PARTITION") or "gpu4,gpu3,gpu2,gpu1,cpu2,cpu1",
        cpus_per_task=_int_env("FACTORIO_AI_SLURM_CPUS_PER_TASK", 8, 1),
        gpus_per_node=min(3, _int_env("FACTORIO_AI_SLURM_GPUS_PER_NODE", 0, 0)),
        time_limit=os.getenv("FACTORIO_AI_SLURM_TIME") or "24:00:00",
        setup_timeout_seconds=_int_env("FACTORIO_AI_SLURM_SETUP_TIMEOUT_SECONDS", 1800, 60),
        task_timeout_seconds=_int_env("FACTORIO_AI_SLURM_TASK_TIMEOUT_SECONDS", 300, 5),
    )


def deploy(cfg: RemoteSlurmConfig | None = None) -> dict[str, Any]:
    cfg = cfg or config()
    remote_dir = resolve_remote_dir(cfg)
    with tempfile.TemporaryDirectory(prefix="factorio-ai-deploy-") as temp_dir:
        archive_path = Path(temp_dir) / "factorio-ai.tar.gz"
        _create_archive(archive_path)
        remote_archive = f"{remote_dir}/factorio-ai.tar.gz"
        _run_scp(archive_path, f"{cfg.user}@{cfg.host}:{remote_archive}", cfg, timeout=cfg.setup_timeout_seconds)

    output = _run_remote(
        f"""set -euo pipefail
REMOTE_DIR={json.dumps(remote_dir)}
ENV_NAME={json.dumps(cfg.conda_env)}
mkdir -p "$REMOTE_DIR"/{{queue,running,results,failed,logs}}
rm -rf "$REMOTE_DIR/factorio-ai"
tar -xzf "$REMOTE_DIR/factorio-ai.tar.gz" -C "$REMOTE_DIR"
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


def submit_worker_job(cfg: RemoteSlurmConfig | None = None) -> dict[str, Any]:
    cfg = cfg or config()
    remote_dir = resolve_remote_dir(cfg)
    deploy(cfg)
    output = _run_remote(
        f"""set -euo pipefail
REMOTE_DIR={json.dumps(remote_dir)}
JOB_NAME={json.dumps(cfg.job_name)}
ENV_NAME={json.dumps(cfg.conda_env)}
PARTITION={json.dumps(cfg.partition)}
CPUS={cfg.cpus_per_task}
GPUS={cfg.gpus_per_node}
TIME_LIMIT={json.dumps(cfg.time_limit)}
mkdir -p "$REMOTE_DIR"/logs
if squeue -h -u "$USER" -n "$JOB_NAME" -t R,PD | awk 'NF{{found=1}} END{{exit !found}}'; then
  echo "already_running=1"
  squeue -h -u "$USER" -n "$JOB_NAME" -t R,PD -o "%i|%T|%M|%L|%R"
  exit 0
fi
job_id="$(sbatch --parsable \\
  --job-name="$JOB_NAME" \\
  --nodes=1 \\
  --ntasks=1 \\
  --cpus-per-task="$CPUS" \\
  $([[ "$GPUS" -gt 0 ]] && printf -- '--gres=gpu:%s ' "$GPUS") \\
  --time="$TIME_LIMIT" \\
  --partition="$PARTITION" \\
  --output="$REMOTE_DIR/logs/%x-%j.out" \\
  --error="$REMOTE_DIR/logs/%x-%j.err" \\
  --export=ALL,ROOT="$REMOTE_DIR",FACTORIO_AI_SLURM_CONDA_ENV="$ENV_NAME" \\
  "$REMOTE_DIR/factorio-ai/slurm/run-factorio-ai-worker.sh")"
echo "submitted_job_id=$job_id"
""",
        cfg,
        timeout=60,
    )
    return {"ok": True, "remoteDir": remote_dir, "output": output}


def status(cfg: RemoteSlurmConfig | None = None) -> dict[str, Any]:
    cfg = cfg or config()
    remote_dir = resolve_remote_dir(cfg)
    output = _run_remote(
        f"""set -euo pipefail
JOB_NAME={json.dumps(cfg.job_name)}
REMOTE_DIR={json.dumps(remote_dir)}
echo "--- jobs ---"
squeue -h -u "$USER" -n "$JOB_NAME" -t R,PD -o "%i|%T|%M|%L|%R" || true
echo "--- counts ---"
for d in queue running results failed logs; do
  mkdir -p "$REMOTE_DIR/$d"
  printf '%s=%s\\n' "$d" "$(find "$REMOTE_DIR/$d" -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' ')"
done
if [[ -f "$REMOTE_DIR/status.txt" ]]; then
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
    remote_dir = resolve_remote_dir(cfg)
    local_env = _llm_env_presence(os.environ)
    probe_code = (
        "import json, os, shutil; "
        f"names = {json.dumps(list(LLM_ENV_VARS))}; "
        "env = {name: bool(os.getenv(name)) for name in names}; "
        "print(json.dumps({'env': env, 'vllm_command': shutil.which('vllm') is not None}, separators=(',', ':')))"
    )
    inner_command = (
        "set -euo pipefail; "
        f"{_attached_env_setup()}"
        f"python3 -c {shlex.quote(probe_code)}"
    )
    try:
        output = _run_remote(
            f"""set -euo pipefail
JOB_NAME={json.dumps(cfg.job_name)}
INNER_COMMAND={json.dumps(inner_command)}
JOB_ID="$(squeue -h -u "$USER" -n "$JOB_NAME" -t R -o "%i" | head -1 | tr -d '[:space:]')"
if [[ -z "$JOB_ID" ]]; then
  echo "{{\\"job_running\\":false}}"
  exit 0
fi
srun --jobid="$JOB_ID" --overlap -N1 -n1 -c1 bash -lc "$INNER_COMMAND" < /dev/null
""",
            cfg,
            timeout=90,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": True,
            "remoteDir": remote_dir,
            "local_env": local_env,
            "remote": {"job_running": False, "error": f"{type(exc).__name__}: {exc}"},
            "llm_ready": False,
            "missing": ["running AUTO job with LLM env"],
            "remediation": _llm_status_remediation(["running AUTO job with LLM env"], cfg, False),
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
    remote_env = remote_payload.get("env") if isinstance(remote_payload.get("env"), dict) else {}
    missing = [
        name
        for name in ("FACTORIO_AI_LLM_BASE_URL", "FACTORIO_AI_LLM_MODEL")
        if not remote_env.get(name)
    ]
    return {
        "ok": True,
        "remoteDir": remote_dir,
        "local_env": local_env,
        "remote": remote_payload,
        "llm_ready": not missing,
        "missing": missing,
        "remediation": _llm_status_remediation(missing, cfg, bool(remote_payload.get("vllm_command"))),
    }


def _llm_status_remediation(
    missing: list[str],
    cfg: RemoteSlurmConfig,
    vllm_available: bool,
) -> dict[str, Any] | None:
    if not missing:
        return None
    return {
        "why": (
            "AUTO Slurm allocation exists, but strategy requests cannot use an LLM until "
            "FACTORIO_AI_LLM_BASE_URL and FACTORIO_AI_LLM_MODEL are visible inside the attached job."
        ),
        "required_remote_env": ["FACTORIO_AI_LLM_BASE_URL", "FACTORIO_AI_LLM_MODEL"],
        "optional_remote_env": ["FACTORIO_AI_LLM_API_KEY", "FACTORIO_AI_LLM_GUIDED_JSON", "FACTORIO_AI_LLM_TIMEOUT"],
        "vllm_available_in_job": vllm_available,
        "example_existing_server": [
            "export FACTORIO_AI_LLM_BASE_URL=http://127.0.0.1:8000/v1",
            "export FACTORIO_AI_LLM_MODEL=<openai-compatible-model-name>",
        ],
        "example_vllm_worker": [
            "export FACTORIO_AI_VLLM_MODEL=<huggingface-or-local-model>",
            "factorio-ai slurm-start-worker",
        ],
        "remote_dir": cfg.remote_dir,
        "job_name": cfg.job_name,
    }


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
    payload = base64.b64encode(json.dumps(task, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii")
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
    available_skills: list[dict[str, Any]] | None = None,
    cfg: RemoteSlurmConfig | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    cfg = cfg or config()
    task = {
        "id": f"strategy-{uuid.uuid4().hex}",
        "type": "strategy_request",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "objective": objective,
            "observation": observation,
            "production_targets": production_targets or {},
            "available_skills": available_skills or [],
        },
    }
    if _use_attached_srun(cfg):
        return _request_task_via_attached_srun(task, cfg, timeout_seconds)

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
    payload = base64.b64encode(json.dumps(task, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii")
    inner_command = (
        "set -euo pipefail; "
        f"{_attached_env_setup()}"
        f"source ~/miniconda3/etc/profile.d/conda.sh && "
        f"conda activate {shlex.quote(cfg.conda_env)} && "
        f"cd {shlex.quote(remote_dir + '/factorio-ai')} && "
        f"python -m factorio_ai.slurm_worker --task {shlex.quote(task_path)} --result {shlex.quote(result_path)}"
    )
    output = _run_remote(
        f"""set -euo pipefail
REMOTE_DIR={json.dumps(remote_dir)}
JOB_NAME={json.dumps(cfg.job_name)}
TASK_NAME={json.dumps(task_name)}
RESULT_NAME={json.dumps(result_name)}
PAYLOAD={json.dumps(payload)}
INNER_COMMAND={json.dumps(inner_command)}
JOB_ID="$(squeue -h -u "$USER" -n "$JOB_NAME" -t R -o "%i" | head -1 | tr -d '[:space:]')"
if [[ -z "$JOB_ID" ]]; then
  echo "__ERROR__:running job not found for $JOB_NAME"
  exit 2
fi
TASK_PATH="$REMOTE_DIR/$TASK_NAME"
RESULT_PATH="$REMOTE_DIR/$RESULT_NAME"
python3 - "$TASK_PATH" "$PAYLOAD" <<'PY'
import base64
import sys
from pathlib import Path

Path(sys.argv[1]).write_bytes(base64.b64decode(sys.argv[2]) + b"\\n")
PY
rm -f "$RESULT_PATH"
srun --jobid="$JOB_ID" --overlap -N1 -n1 -c1 bash -lc "$INNER_COMMAND" < /dev/null
cat "$RESULT_PATH"
rm -f "$TASK_PATH" "$RESULT_PATH"
""",
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


def _llm_env_presence(env: Any) -> dict[str, bool]:
    return {name: bool(env.get(name)) for name in LLM_ENV_VARS}


def _attached_env_setup() -> str:
    commands = []
    for name in LLM_ENV_VARS:
        value = os.getenv(name)
        if value:
            commands.append(f"export {name}={shlex.quote(value)}")
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
