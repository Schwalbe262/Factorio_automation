# One-click: free remote worker disk (safe — old logs + completed task dirs + stale archive; running
# vLLM services keep their GPUs and heartbeats) then deploy the latest code so the cluster node gets
# the new code-agent handler (enables FLE). Run via the Claude prompt:
#   ! powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\NEC\Documents\Factorio\cleanup_and_deploy.ps1
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$key = "C:\Users\NEC\.ssh\r1jae262_lf.pem"
$rh  = "r1jae262@172.16.10.37"

Write-Host "=== 1/3 worker disk before ==="
ssh -i $key -o StrictHostKeyChecking=no $rh "cd ~/factorio-ai-worker; du -sh . ; du -sh * .[^.]* 2>/dev/null | sort -h | tail -6; df -h ~ | tail -1"

Write-Host "=== 2/3 safe cleanup (old logs >60min + completed task dirs >60min + stale archive) ==="
$cleanup = @'
cd ~/factorio-ai-worker || exit 0
find logs factorio-ai/logs -type f -mmin +60 -delete 2>/dev/null
find .factorio-ai-scheduler-tasks -maxdepth 1 -type d -mmin +60 -exec rm -rf {} + 2>/dev/null
find factorio-ai/.factorio-ai-scheduler-tasks -maxdepth 1 -type f \( -name 'strategy-*.json' -o -name 'layout-improvement-*.json' -o -name '.layout-improvement-*.tmp' \) -mmin +180 -delete 2>/dev/null
rm -f factorio-ai.tar.gz
echo 'after:'
du -sh .
df -h ~ | tail -1
'@
$cleanup | ssh -i $key -o StrictHostKeyChecking=no $rh "bash -s"

Write-Host "=== 3/3 deploy (pushes the code-agent handler to the node) ==="
$env:FACTORIO_AI_SLURM_ENABLED = "1"
$env:FACTORIO_AI_SLURM_MODE = "scheduler"
$env:FACTORIO_AI_SLURM_SCHEDULER_URL = "http://100.112.168.31:8000"
$env:FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT = "r1jae262"
$env:SUPERCOMPUTER_WORKER_SSH_KEY = $key
$env:FACTORIO_AI_SLURM_REMOTE_DIR = "~/factorio-ai-worker"
$env:PYTHONPATH = "src"
python -m factorio_ai.cli slurm-deploy
