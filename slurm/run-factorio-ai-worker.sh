#!/usr/bin/env bash
#SBATCH --job-name=factorio-ai-worker
#SBATCH --output=logs/factorio-ai-worker-%j.out
#SBATCH --error=logs/factorio-ai-worker-%j.err
#SBATCH --time=24:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=0

set -euo pipefail

ROOT_RAW="${ROOT:-${FACTORIO_AI_SLURM_REMOTE_DIR:-${SUPERCOMPUTER_WORKER_REMOTE_DIR:-$PWD}}}"
if [[ "$ROOT_RAW" == "~" ]]; then
  ROOT="$HOME"
elif [[ "$ROOT_RAW" == "~/"* ]]; then
  ROOT="$HOME/${ROOT_RAW:2}"
elif [[ "$ROOT_RAW" == /* ]]; then
  ROOT="$ROOT_RAW"
else
  ROOT="$PWD/$ROOT_RAW"
fi

if [[ -f "$ROOT/config.env" ]]; then
  while IFS='=' read -r key value; do
    [[ -n "$key" && "$key" == FACTORIO_AI_* ]] || continue
    value="${value//\\n/$'\n'}"
    export "$key=$value"
  done < "$ROOT/config.env"
fi

ENV_NAME="${FACTORIO_AI_SLURM_CONDA_ENV:-factorio-ai}"
POLL_SECONDS="${FACTORIO_AI_WORKER_POLL_SECONDS:-1}"

mkdir -p "$ROOT"/{queue,running,results,failed,logs}
cd "$ROOT/factorio-ai"

if command -v conda >/dev/null 2>&1; then
  eval "$(conda shell.bash hook)"
  if ! conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
    conda create -y -n "$ENV_NAME" python=3.10
  fi
  conda activate "$ENV_NAME"
fi

GPU_LIST=""
GPU_ERROR=""
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_LIST="$(nvidia-smi -L 2>/dev/null || true)"
  if [[ -z "$GPU_LIST" ]]; then
    GPU_ERROR="$(nvidia-smi -L 2>&1 >/dev/null || true)"
  fi
else
  GPU_ERROR="nvidia-smi not found"
fi

if [[ -n "${FACTORIO_AI_VLLM_MODEL:-}" ]]; then
  if [[ -n "${FACTORIO_AI_HF_HOME:-}" ]]; then
    export HF_HOME="$FACTORIO_AI_HF_HOME"
  else
    export HF_HOME="$HOME/factorio-ai-models"
  fi
  export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
  mkdir -p "$HUGGINGFACE_HUB_CACHE"
  VLLM_PORT="${FACTORIO_AI_VLLM_PORT:-8000}"
  export FACTORIO_AI_LLM_BASE_URL="${FACTORIO_AI_LLM_BASE_URL:-http://127.0.0.1:${VLLM_PORT}/v1}"
  export FACTORIO_AI_LLM_MODEL="${FACTORIO_AI_LLM_MODEL:-$FACTORIO_AI_VLLM_MODEL}"
  if [[ -n "${FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER:-}" ]]; then
    export VLLM_USE_FLASHINFER_SAMPLER="$FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER"
  fi
  if [[ -z "$GPU_LIST" ]]; then
    echo "vllm_gpu_unavailable=1"
    echo "gpu_error=$GPU_ERROR"
    exit 2
  fi
  if ! command -v vllm >/dev/null 2>&1; then
    echo "vllm_not_found=1"
    exit 2
  fi
  vllm serve "$FACTORIO_AI_VLLM_MODEL" \
    --host 127.0.0.1 \
    --port "$VLLM_PORT" \
    ${FACTORIO_AI_VLLM_ARGS:-} > "$ROOT/logs/vllm-${SLURM_JOB_ID:-local}.out" 2> "$ROOT/logs/vllm-${SLURM_JOB_ID:-local}.err" &
  VLLM_PID="$!"
  trap 'kill "$VLLM_PID" 2>/dev/null || true' EXIT
  sleep "${FACTORIO_AI_VLLM_STARTUP_SECONDS:-30}"
fi

echo "job_name=${SLURM_JOB_NAME:-factorio-ai-worker}"
echo "job_id=${SLURM_JOB_ID:-local}"
echo "root=$ROOT"
echo "env=$ENV_NAME"
echo "cuda_visible_devices=${CUDA_VISIBLE_DEVICES:-}"
echo "slurm_job_gpus=${SLURM_JOB_GPUS:-}"
echo "slurm_step_gpus=${SLURM_STEP_GPUS:-}"
echo "gpu_list=${GPU_LIST//$'\n'/;}"
echo "gpu_error=$GPU_ERROR"
echo "llm_base_url=${FACTORIO_AI_LLM_BASE_URL:-}"
echo "llm_model=${FACTORIO_AI_LLM_MODEL:-}"

python -m factorio_ai.slurm_worker --root "$ROOT" --poll-seconds "$POLL_SECONDS"
