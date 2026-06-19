# Direct autopilot launcher (bypasses the supervisor's LLM-ready gate). The autopilot's graceful
# degradation runs the deterministic heuristic when the remote LLM isn't ready, so the factory keeps
# progressing even while the cluster serving is cold-loading / GPU-contended. Use when the supervisor
# is stuck waiting on serving. Stop the supervisor's own autopilot first to avoid two RCON drivers.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$env:FACTORIO_AI_SLURM_ENABLED = "1"
$env:FACTORIO_AI_SLURM_MODE = "scheduler"
$env:FACTORIO_AI_SLURM_SCHEDULER_URL = "http://100.112.168.31:8000"
$env:FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT = "r1jae262"
$env:SUPERCOMPUTER_WORKER_SSH_KEY = "C:\Users\NEC\.ssh\r1jae262_lf.pem"
$env:FACTORIO_AI_SLURM_REMOTE_DIR = "~/factorio-ai-worker"
$env:FACTORIO_AI_SLURM_TASK_TIMEOUT_SECONDS = "900"
$env:FACTORIO_AI_VLLM_MODEL = "QuantTrio/Qwen3.6-27B-AWQ"
$env:FACTORIO_AI_LLM_MODEL = "QuantTrio/Qwen3.6-27B-AWQ"
$env:FACTORIO_AI_LLM_GUIDED_JSON = "1"
$env:FACTORIO_AI_LLM_MAX_TOKENS = "2048"
$env:FACTORIO_AI_REMOTE_STRATEGY_TIMEOUT_SECONDS = "120"
$env:FACTORIO_AI_AUTOPILOT_LLM_DEGRADE_CYCLES = "1"      # degrade fast when LLM not ready
$env:FACTORIO_AI_AUTOPILOT_SLURM_AUTO_RENEW_ENABLED = "0"
$env:FACTORIO_AI_SLURM_AUTO_RENEW_ENABLED = "0"
$env:PYTHONPATH = "src"
python -m factorio_ai.cli run-no-mod-autopilot --objective launch_rocket_program --require-llm
