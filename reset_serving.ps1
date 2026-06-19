# One-shot serving reset: cancel the stale/orphaned vLLM services (they hold a6000 GPUs but post no
# "ready" heartbeat) and allocate fresh ones, then print readiness. Run this yourself via the `!`
# prefix in the Claude prompt:  ! powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\NEC\Documents\Factorio\reset_serving.ps1
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$env:FACTORIO_AI_SLURM_ENABLED = "1"
$env:FACTORIO_AI_SLURM_MODE = "scheduler"
$env:FACTORIO_AI_SLURM_SCHEDULER_URL = "http://100.112.168.31:8000"
$env:FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT = "r1jae262"
$env:SUPERCOMPUTER_WORKER_SSH_KEY = "C:\Users\NEC\.ssh\r1jae262_lf.pem"
$env:FACTORIO_AI_SLURM_REMOTE_DIR = "~/factorio-ai-worker"
$env:FACTORIO_AI_VLLM_MODEL = "QuantTrio/Qwen3.6-27B-AWQ"
$env:FACTORIO_AI_LLM_MODEL = "QuantTrio/Qwen3.6-27B-AWQ"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_ENABLED = "1"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_COUNT = "4"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_MAX_COUNT = "4"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_DURATION_SECONDS = "43200"
$env:FACTORIO_AI_VLLM_PORT = "8000"
$env:FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL = "a6000"
$env:FACTORIO_AI_LLM_GUIDED_JSON = "1"
$env:PYTHONPATH = "src"

Write-Host "=== 1/3 cancel stale vLLM services ==="
python -m factorio_ai.cli slurm-cancel-vllm-services
Start-Sleep 5
Write-Host "=== 2/3 ensure a fresh vLLM service ==="
python -m factorio_ai.cli slurm-ensure-vllm-service
Start-Sleep 5
Write-Host "=== 3/3 status (re-run after ~3-5 min for the 27B to load to 'ready') ==="
python -m factorio_ai.cli slurm-vllm-service-status
