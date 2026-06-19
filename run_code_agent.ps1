# Launch the FLE-style code-generation agent (run-no-mod-code-agent) with the same cluster/serving
# environment the unattended autopilot supervisor uses. Additive: this drives the SAME game agent as
# the strategy-autopilot, so do NOT run both at once -- stop the autopilot first if you want the
# code-agent to have exclusive control.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# --- cluster / serving env (mirrors run_factorio_no_mod_unattended_llm.ps1) ---
$env:FACTORIO_AI_SLURM_ENABLED = "1"
$env:FACTORIO_AI_SLURM_MODE = "scheduler"
$env:FACTORIO_AI_SLURM_SCHEDULER_URL = "http://100.112.168.31:8000"
$env:FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT = "r1jae262"
$env:SUPERCOMPUTER_WORKER_SSH_KEY = "C:\Users\NEC\.ssh\r1jae262_lf.pem"
$env:FACTORIO_AI_SLURM_REMOTE_DIR = "~/factorio-ai-worker"
$env:FACTORIO_AI_SLURM_TASK_TIMEOUT_SECONDS = "900"
$env:FACTORIO_AI_LLM_GUIDED_JSON = "1"
$env:FACTORIO_AI_LLM_MAX_TOKENS = "2048"
$env:FACTORIO_AI_LLM_TIMEOUT = "600"
$env:PYTHONPATH = "src"

$cycles = if ($args.Count -ge 1) { $args[0] } else { "0" }  # 0 = run until interrupted
Write-Host "Starting code-gen agent (cycles=$cycles). Stop the strategy-autopilot first to avoid two drivers."
python -m factorio_ai.cli run-no-mod-code-agent --objective launch_rocket_program --cycles $cycles
