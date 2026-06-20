param(
    [string]$Objective = "launch_rocket_program",
    [int]$CheckSeconds = 30,
    [int]$SchedulerCheckSeconds = 60,
    [int]$AutopilotStaleSeconds = 900,
    [int]$AutopilotSleepSeconds = 12,
    [int]$IdleLoopStaleSeconds = 180,
    [int]$LayoutMaxActiveTasks = 1,
    [int]$FoundryStaleSeconds = 900,
    # Raised 20 -> 1800: the skill-foundry makes 4-6 min LLM calls; at a 20s sleep it hammered the
    # 27B almost continuously and STARVED the autopilot's strategy decisions (autopilot froze at
    # cycle_start retrying timed-out calls). 30 min between foundry cycles keeps it occasional so the
    # primary autopilot loop gets reliable LLM access (matters for the long rocket grind).
    [int]$FoundrySleepSeconds = 1800,
    [int]$ServerSaveIntervalSeconds = 300,
    [int]$LlmReadyGraceSeconds = 300,
    [switch]$NoDashboard
)

$ErrorActionPreference = "Continue"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

$env:PYTHONPATH = "src"
$env:FACTORIO_AI_SLURM_ENABLED = "1"
$env:FACTORIO_AI_SLURM_MODE = "scheduler"
$env:FACTORIO_AI_SLURM_SCHEDULER_URL = "http://100.112.168.31:8000"
$env:FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT = "r1jae262"
# The default ~/.ssh/r1jae262.pem has CRLF line endings (Windows-mangled) which makes
# OpenSSH fail to parse it ("error in libcrypto") -> all SSH ops (strategy/foundry
# payload upload, deploy) silently fail. r1jae262_lf.pem is the LF-normalized copy.
$env:SUPERCOMPUTER_WORKER_SSH_KEY = "C:\Users\NEC\.ssh\r1jae262_lf.pem"
$env:FACTORIO_AI_SLURM_REMOTE_DIR = "~/factorio-ai-worker"
$env:FACTORIO_AI_SLURM_TASK_TIMEOUT_SECONDS = "900"
$env:FACTORIO_AI_SLURM_SCHEDULER_CPUS = "3"
$env:FACTORIO_AI_SLURM_SCHEDULER_GPUS = "1"
# Our account OWNS 8 a6000 GPUs (sched_owned=8) and a10 shows sched_owned=0 (we can't actually get
# a10 even though the cluster has 54 free), so target a6000 directly. This is where COUNT=4 lives.
# Revert to "a6000ada,a6000" only if a6000ada capacity is later granted to the account.
$env:FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL = "a6000"
$env:FACTORIO_AI_SLURM_SCHEDULER_PRIORITY = "100"
$env:FACTORIO_AI_SLURM_LAYOUT_GPU_MODELS = "a6000ada,a6000"
$env:FACTORIO_AI_SLURM_LAYOUT_CPUS = "3"
$env:FACTORIO_AI_SLURM_LAYOUT_PRIORITY = "80"
# Qwen3.6-27B at 4-bit AWQ (~14GB) fits one A6000 and runs on Ampere+Ada (Marlin), unlike FP8
# which crashed on this node's GPUs (exit 1). Two instances (one per GPU) below.
$env:FACTORIO_AI_VLLM_MODEL = "QuantTrio/Qwen3.6-27B-AWQ"
# max-model-len 16384 (was 12288): the skill_foundry codegen prompt is ~9.4k input tokens and the
# 27B reasoning model writes chain-of-thought + a full module, so 12288 left only ~2.9k output budget
# -> the JSON got truncated mid-string ("LLM response content is not a JSON object"). 16384 gives
# ~7k output budget so foundry codegen + reasoning fit; safe on the a6000 (48GB) at 0.90 util
# (27B AWQ ~15GB weights + paged KV-cache). Still well under the 32768 that crashed.
$env:FACTORIO_AI_VLLM_ARGS = "--max-model-len 16384 --gpu-memory-utilization 0.90 --quantization awq --enforce-eager"
# Persistent model cache OUTSIDE ~/.cache (which the cluster may purge): the ~15GB AWQ
# download survives reboots/purges and is never re-fetched. Use an ABSOLUTE path -- a
# leading ~ does NOT expand inside the quoted `export HF_HOME=...` on the node.
$env:FACTORIO_AI_HF_HOME = "/home1/r1jae262/factorio-ai-models"
$env:FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER = "0"
$env:FACTORIO_AI_VLLM_PORT = "8000"
$env:FACTORIO_AI_VLLM_STARTUP_SECONDS = "1800"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_ENABLED = "1"
# DYNAMIC 1..MAX warm 27B-AWQ instances, each on a DISTINCT localhost port (8000,8001,8002,8003);
# clients round-robin + co-locate per-service to that service's node. The supervisor scales the
# live service count to the GPUs actually grantable each cycle (target = clamp(running + free a6000,
# 1, MAX_COUNT)), so it uses 4 GPUs when free and gracefully drops to 1-3 under contention instead
# of leaving a service wedged in the queue. Allocation-leak fix (commit 38d6b8e) makes churn
# self-cleaning, so this can never wedge the account; worst case is a smaller live count.
# 2026-06-19: dropped 4 -> 1. COUNT=4 put 4 vLLM services on the SAME contended allocation (n101),
# all loading the 27B (~16GB each) simultaneously -> disk/CPU/RAM I/O starvation -> each load blew
# past the startup timeout -> every service died ("vLLM endpoint not ready before startup timeout") ->
# serving stayed down. A single service loads one model on one GPU, comes up within the timeout, and
# stays up. FLE/strategy LLM calls are serial anyway, so COUNT=1 costs little. Scale back up to 4 only
# once services reliably SPREAD across idle nodes (n103/n106), not pile on one.
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_COUNT = "1"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_MAX_COUNT = "1"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_DYNAMIC = "0"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_DURATION_SECONDS = "43200"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_HEARTBEAT_SECONDS = "30"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_STALE_SECONDS = "120"
# Raised 180 -> 1800: under cluster GPU contention the scheduler can take several minutes to place
# a queued service; the old 180s window cancelled+resubmitted it before it could land (churn ->
# zero serving). 1800s lets a single service wait in queue for a free GPU.
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_QUEUE_STALE_SECONDS = "1800"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_CPUS = "1"
$env:FACTORIO_AI_SCHEDULER_VLLM_CLIENT_CPUS = "1"
$env:FACTORIO_AI_SCHEDULER_VLLM_CLIENT_GPUS = "0"
# Raised 120 -> 300 to overcome the "(Priority)" pending state: the a6000 pool is contended and our
# single service was queued behind higher-priority work, unable to claim a free node. Higher priority
# lets it grab a free a6000 node (e.g. n106). Lower back toward 120 once contention eases.
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_PRIORITY = "300"
$env:FACTORIO_AI_REQUIRE_LLM_STRATEGY = "1"
# Default to strict local-LLM operation: if scheduler/vLLM is unavailable, pause autopilot instead
# of silently switching to heuristic strategy. Set to 1 only for an explicitly degraded run.
if (-not $env:FACTORIO_AI_ALLOW_HEURISTIC_AUTOPILOT_FALLBACK) {
    $env:FACTORIO_AI_ALLOW_HEURISTIC_AUTOPILOT_FALLBACK = "0"
}
$env:FACTORIO_AI_LLM_GUIDED_JSON = "1"
# Qwen3.6 emits a verbose chain-of-thought when unconstrained, overflowing the old 512-token
# cap so the JSON never closes ("LLM response content is not a JSON object"). guided_json above
# forces the strict schema (no rambling); this gives the schema fields room to complete.
$env:FACTORIO_AI_LLM_MAX_TOKENS = "2048"
$env:FACTORIO_AI_LLM_TIMEOUT = "600"
$env:FACTORIO_AI_REMOTE_STRATEGY_TIMEOUT_SECONDS = "900"
# 2026-06-19: DISABLED temporarily. With COUNT=1 serving, the background layout loop (INTERVAL=0 =
# continuous) saturated the single GPU -> the autopilot's per-cycle strategy LLM call queued ~105s
# behind layout jobs -> the game crawled at ~100s/cycle, far too slow to reach a rocket. Layout is
# background optimization, not rocket-critical, so pause it to give the autopilot fast exclusive
# serving. Re-enable (="1") once serving has the throughput (e.g. staggered COUNT>1) to run both.
$env:FACTORIO_AI_BACKGROUND_LAYOUT_ENABLED = "0"
$env:FACTORIO_AI_BACKGROUND_LAYOUT_MODE = "scheduler"
$env:FACTORIO_AI_BACKGROUND_LAYOUT_MAX_ACTIVE_TASKS = [string]$LayoutMaxActiveTasks
$env:FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS = "0"
$env:FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES = "360"
$env:FACTORIO_AI_SLURM_RENEW_CHECK_INTERVAL_SECONDS = "1800"
# Self-development (skill foundry): the local Qwen authors missing executors and each
# candidate is exercised on a throwaway COPY of the live save before it can run live.
# 2026-06-19: DISABLED temporarily alongside the layout loop so the single GPU serves the autopilot's
# strategy calls exclusively (foundry requests were ~300s and stalled the autopilot's per-cycle LLM
# call). Re-enable (="1") once serving has the throughput (staggered COUNT>1) to run background loops.
$env:FACTORIO_AI_SKILL_FOUNDRY_ENABLED = "0"
$env:FACTORIO_AI_FOUNDRY_SANDBOX_ENABLED = "1"
$env:FACTORIO_AI_FOUNDRY_SANDBOX_SERVER_PORT = "34297"
$env:FACTORIO_AI_FOUNDRY_SANDBOX_RCON_PORT = "27115"
$env:FACTORIO_AI_FOUNDRY_SANDBOX_RCON_TIMEOUT = "180"
$env:FACTORIO_AI_FOUNDRY_MAX_ATTEMPTS = "3"
# Raised 3072 -> 5120 alongside max-model-len 16384: the reasoning model needs room for
# chain-of-thought + the full module so the JSON isn't truncated. 9.4k prompt + 5120 < 16384.
$env:FACTORIO_AI_FOUNDRY_MAX_TOKENS = "5120"
# Self-repair: let the local LLM generate sandbox-gated overrides for hand-written skills that keep
# failing live (e.g. setup_power on a new map). Auto-applied after gates; auto-rolls back to the
# original on regression. Requires the sandbox gate above.
$env:FACTORIO_AI_SKILL_REPAIR_ENABLED = "1"
$env:FACTORIO_AI_IMPL_REPAIR_FAIL_LIMIT = "3"

$runtimeDir = Join-Path $repoRoot "runtime"
$logDir = Join-Path $repoRoot "logs"
New-Item -ItemType Directory -Force -Path $runtimeDir, $logDir | Out-Null

$supervisorLog = Join-Path $logDir "unattended-llm-supervisor.log"
$statusPath = Join-Path $runtimeDir "unattended-llm-supervisor.json"
$lastSchedulerCheck = [DateTime]::MinValue
$lastServerSave = [DateTime]::MinValue
$lastLlmReadyAt = [DateTime]::MinValue
$lastSchedulerStatus = $null
$lastVllmServiceStatus = $null

# Driver selection: which loop actually plays the game.
#   "autopilot"  (default) = deterministic strategy + hand-written skills (run-no-mod-autopilot)
#   "code-agent" / "fle"   = FLE-style: the LLM writes a Python program each step (run-no-mod-code-agent)
# Set $env:FACTORIO_AI_DRIVER="code-agent" before launching to drive with FLE. Default is unchanged.
$Driver = if ($env:FACTORIO_AI_DRIVER) { $env:FACTORIO_AI_DRIVER.Trim().ToLower() } else { "autopilot" }
if ($Driver -in @("code-agent", "code_agent", "codeagent", "fle")) {
    $Driver = "code-agent"
    $DriverCli = "run-no-mod-code-agent"
    $DriverHeartbeatName = "code-agent-heartbeat.json"
} else {
    $Driver = "autopilot"
    $DriverCli = "run-no-mod-autopilot"
    $DriverHeartbeatName = "autopilot-heartbeat.json"
}
Write-Host "[factorio-ai] driver = $Driver ($DriverCli)"

function Write-SupervisorLog {
    param([string]$Message)
    $line = "$(Get-Date -Format o) $Message"
    Add-Content -Path $supervisorLog -Value $line -Encoding UTF8
    Write-Host $line
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Get-JsonAgeSeconds {
    param($Json)
    if ($null -eq $Json -or -not $Json.updated_at) {
        return [double]::PositiveInfinity
    }
    try {
        $timestamp = [DateTimeOffset]::Parse([string]$Json.updated_at)
        return ([DateTimeOffset]::UtcNow - $timestamp.ToUniversalTime()).TotalSeconds
    } catch {
        return [double]::PositiveInfinity
    }
}

function Test-PidAlive {
    param($PidValue)
    if ($null -eq $PidValue) {
        return $false
    }
    try {
        $pidNumber = [int]$PidValue
    } catch {
        return $false
    }
    return $null -ne (Get-Process -Id $pidNumber -ErrorAction SilentlyContinue)
}

function Test-HeuristicAutopilotFallbackAllowed {
    return $Driver -eq "autopilot" -and $env:FACTORIO_AI_ALLOW_HEURISTIC_AUTOPILOT_FALLBACK -notin @("0", "false", "False", "FALSE", "no", "off")
}

function Test-ProcessRequiresLlm {
    param($Process)
    if ($null -eq $Process -or -not $Process.CommandLine) {
        return $false
    }
    return [string]$Process.CommandLine -like "*--require-llm*"
}

function Get-ManagedProcesses {
    param([string]$Needle)
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -like "python*" -and
            $_.CommandLine -and
            $_.CommandLine -like "*$Needle*" -and
            $_.ProcessId -ne $PID
        }
}

function Stop-ManagedProcesses {
    param(
        [string]$Name,
        [string]$Needle
    )
    $processes = @(Get-ManagedProcesses $Needle)
    if ($processes.Count -eq 0) {
        return
    }
    Write-SupervisorLog "stopping $Name while waiting for scheduler local LLM readiness"
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Start-ManagedPython {
    param(
        [string]$Name,
        [string[]]$Arguments
    )
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdout = Join-Path $logDir "$Name.$stamp.stdout.log"
    $stderr = Join-Path $logDir "$Name.$stamp.stderr.log"
    Write-SupervisorLog "starting $Name"
    return Start-Process -FilePath "python" `
        -ArgumentList $Arguments `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
}

function Invoke-Cli {
    param([string[]]$Arguments)
    & python @Arguments | Out-Null
    return $LASTEXITCODE
}

function Invoke-CliJson {
    param([string[]]$Arguments)
    try {
        $output = & python @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        $text = ($output | Out-String).Trim()
        if (-not $text) {
            return [ordered]@{
                ok = ($exitCode -eq 0)
                exit_code = $exitCode
            }
        }
        try {
            return $text | ConvertFrom-Json
        } catch {
            return [ordered]@{
                ok = $false
                exit_code = $exitCode
                error = $text
            }
        }
    } catch {
        return [ordered]@{
            ok = $false
            error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
        }
    }
}

function Compact-SchedulerStatus {
    param($Status)
    if ($null -eq $Status) {
        return $null
    }
    $remote = $Status.remote
    return [ordered]@{
        ok = $Status.ok
        provider = $Status.provider
        llm_ready = $Status.llm_ready
        missing = @($Status.missing)
        vllm_model = $env:FACTORIO_AI_VLLM_MODEL
        vllm_args = $env:FACTORIO_AI_VLLM_ARGS
        vllm_startup_seconds = $env:FACTORIO_AI_VLLM_STARTUP_SECONDS
        llm_timeout_seconds = $env:FACTORIO_AI_LLM_TIMEOUT
        remote_strategy_timeout_seconds = $env:FACTORIO_AI_REMOTE_STRATEGY_TIMEOUT_SECONDS
        slurm_task_timeout_seconds = $env:FACTORIO_AI_SLURM_TASK_TIMEOUT_SECONDS
        schedulerUrl = $Status.schedulerUrl
        account = $Status.account
        scheduler_gpu_model_env = $env:FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL
        scheduler_cpus = $env:FACTORIO_AI_SLURM_SCHEDULER_CPUS
        scheduler_priority = $env:FACTORIO_AI_SLURM_SCHEDULER_PRIORITY
        layout_max_active_tasks = $LayoutMaxActiveTasks
        gpu_model_candidates = @($remote.gpu_model_candidates)
        selected_gpu_model = $remote.selected_gpu_model
        scheduler_ready_free_gpus = $remote.scheduler_ready_free_gpus
        scheduler_ready_gpu_slots = $remote.scheduler_ready_gpu_slots
        scheduler_gpu_queue_capacity = $remote.scheduler_gpu_queue_capacity
        pending_gpu_tasks = $remote.pending_gpu_tasks
        resource_fit_pending_gpu_tasks = $remote.resource_fit_pending_gpu_tasks
        active_layout_tasks = $remote.active_layout_tasks
        active_layout_capacity_remaining = $remote.active_layout_capacity_remaining
        checked_at = [DateTimeOffset]::UtcNow.ToString("o")
    }
}

function Compact-VllmServiceStatus {
    param($Status)
    if ($null -eq $Status) {
        return $null
    }
    $serviceStatus = $Status
    if ($Status.status) {
        $serviceStatus = $Status.status
    }
    return [ordered]@{
        ok = $Status.ok
        action = $Status.action
        service_ready = $serviceStatus.service_ready
        missing = @($serviceStatus.missing)
        vllm_model = $serviceStatus.vllm_model
        duration_seconds = $serviceStatus.duration_seconds
        heartbeat_age_seconds = $serviceStatus.heartbeat_age_seconds
        heartbeat = $serviceStatus.heartbeat
        active_services = @($serviceStatus.active_services)
        checked_at = [DateTimeOffset]::UtcNow.ToString("o")
    }
}

function Ensure-NoModServer {
    # Quiet probe: when no server is running yet, no-mod-observe raises ConnectionRefused and prints a
    # full traceback. That is expected on first boot (we just start the server), so suppress the noise
    # and only act on the exit code.
    & python -m factorio_ai.cli no-mod-observe *> $null
    $observeExit = $LASTEXITCODE
    if ($observeExit -eq 0) {
        return
    }
    Write-SupervisorLog "no-mod RCON not reachable yet; ensuring save and starting server"
    Invoke-Cli @("-m", "factorio_ai.cli", "create-no-mod-save") | Out-Null
    Start-ManagedPython "unattended-no-mod-server" @("-m", "factorio_ai.cli", "start-no-mod-server") | Out-Null
    Start-Sleep -Seconds 12
}

function Ensure-NoModServerSave {
    # Periodically persist the live world to its save file so a restart resumes instead of reloading
    # the original map. The server is started with --start-server <save> and never writes it back on
    # its own, so without this every restart begins from scratch on the same seed.
    if ($ServerSaveIntervalSeconds -le 0) {
        return
    }
    $now = Get-Date
    if (($now - $script:lastServerSave).TotalSeconds -lt $ServerSaveIntervalSeconds) {
        return
    }
    & python -m factorio_ai.cli no-mod-server-save *> $null
    if ($LASTEXITCODE -eq 0) {
        $script:lastServerSave = $now
        Write-SupervisorLog "no-mod server state saved (/server-save)"
    }
}

function Ensure-LayoutCapacityLimit {
    if ($LayoutMaxActiveTasks -lt 1) {
        return
    }
    $settingsPath = Join-Path $runtimeDir "layout-llm-settings.json"
    $settings = Read-JsonFile $settingsPath
    if ($settings -and [int]$settings.max_active_layout_tasks -eq $LayoutMaxActiveTasks) {
        return
    }
    $payload = [ordered]@{
        max_active_layout_tasks = $LayoutMaxActiveTasks
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
        source = "unattended-llm-supervisor"
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $settingsPath -Encoding UTF8
    Write-SupervisorLog "set layout max active scheduler tasks to $LayoutMaxActiveTasks for unattended strategy capacity"
}

function Ensure-Dashboard {
    if ($NoDashboard) {
        return
    }
    $listener = Get-NetTCPConnection -State Listen -LocalPort 18889 -ErrorAction SilentlyContinue
    if ($listener) {
        return
    }
    Start-ManagedPython "unattended-dashboard" @(
        "-m", "factorio_ai.cli", "web",
        "--host", "0.0.0.0",
        "--port", "18889",
        "--objective", $Objective
    ) | Out-Null
}

function Ensure-SchedulerLlm {
    $now = Get-Date
    if (($now - $lastSchedulerCheck).TotalSeconds -lt $SchedulerCheckSeconds) {
        return
    }
    $script:lastSchedulerCheck = $now
    Write-SupervisorLog "checking scheduler-managed local LLM"
    Invoke-Cli @("-m", "factorio_ai.cli", "slurm-ensure-worker", "--renew-before-minutes", $env:FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES) | Out-Null
    if ($env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_ENABLED -notin @("0", "false", "False", "FALSE", "no", "off")) {
        $script:lastVllmServiceStatus = Compact-VllmServiceStatus (
            Invoke-CliJson @(
                "-m", "factorio_ai.cli", "slurm-ensure-vllm-service",
                "--duration-seconds", $env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_DURATION_SECONDS
            )
        )
        if ($null -eq $script:lastVllmServiceStatus -or $script:lastVllmServiceStatus.service_ready -ne $true) {
            $missingService = @($script:lastVllmServiceStatus.missing) -join ", "
            if (-not $missingService) {
                $missingService = "service starting"
            }
            Write-SupervisorLog "scheduler vLLM service is not ready yet ($missingService); supervisor will retry"
        }
    }
    $script:lastSchedulerStatus = Compact-SchedulerStatus (Invoke-CliJson @("-m", "factorio_ai.cli", "slurm-llm-status"))
    if ($null -eq $script:lastSchedulerStatus -or $script:lastSchedulerStatus.llm_ready -ne $true) {
        $missing = @($script:lastSchedulerStatus.missing) -join ", "
        if (-not $missing) {
            $missing = "unknown"
        }
        Write-SupervisorLog "scheduler local LLM is not ready yet ($missing); supervisor will retry"
    }
}

function Test-SchedulerLlmReady {
    if ($null -eq $script:lastSchedulerStatus -or $script:lastSchedulerStatus.llm_ready -ne $true) {
        # Ride through transient scheduler-API blips: if the LLM was confirmed ready very recently,
        # keep the loops alive instead of tearing them down on a single failed/timed-out status check.
        if ($script:lastLlmReadyAt -ne [DateTime]::MinValue -and ((Get-Date) - $script:lastLlmReadyAt).TotalSeconds -lt $LlmReadyGraceSeconds) {
            return $true
        }
        return $false
    }

    $script:lastLlmReadyAt = Get-Date
    if (
        $env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_ENABLED -notin @("0", "false", "False", "FALSE", "no", "off") -and
        -not ($null -ne $script:lastVllmServiceStatus -and $script:lastVllmServiceStatus.service_ready -eq $true)
    ) {
        Write-SupervisorLog "scheduler local LLM is ready; ignoring stale vLLM service heartbeat gate"
    }
    return $true
}

function Test-AutopilotActiveCycle {
    $heartbeat = Read-JsonFile (Join-Path $runtimeDir $DriverHeartbeatName)
    if ($null -eq $heartbeat -or $heartbeat.state -ne "cycle_start") {
        return $false
    }
    if ($heartbeat.pid -and -not (Test-PidAlive $heartbeat.pid)) {
        return $false
    }
    return (Get-JsonAgeSeconds $heartbeat) -lt $AutopilotStaleSeconds
}

function Write-AutopilotWaitingHeartbeat {
    $existing = Read-JsonFile (Join-Path $runtimeDir $DriverHeartbeatName)
    $cycle = 0
    if ($existing -and $existing.cycle) {
        try {
            $cycle = [int]$existing.cycle
        } catch {
            $cycle = 0
        }
    }
    $payload = [ordered]@{
        active = $true
        state = "waiting_for_scheduler_llm"
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
        objective = $Objective
        cycle = $cycle
        reason = "waiting for scheduler local LLM readiness"
        pid = $null
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runtimeDir $DriverHeartbeatName) -Encoding UTF8
}

function Write-IdleLayoutWaitingHeartbeat {
    $payload = [ordered]@{
        pid = $null
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
        objective = $Objective
        state = "waiting_for_scheduler_llm"
        reason = "waiting for scheduler local LLM readiness"
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runtimeDir "idle-layout-loop.json") -Encoding UTF8
}

function Ensure-IdleLayoutLoop {
    if ($env:FACTORIO_AI_BACKGROUND_LAYOUT_ENABLED -in @("0", "false", "False", "FALSE", "no", "off")) {
        Stop-ManagedProcesses "idle-layout-loop" "run-no-mod-idle-layout-loop"
        Write-IdleLayoutWaitingHeartbeat
        return
    }
    if (-not (Test-SchedulerLlmReady)) {
        Stop-ManagedProcesses "idle-layout-loop" "run-no-mod-idle-layout-loop"
        Write-IdleLayoutWaitingHeartbeat
        return
    }
    $existing = Get-ManagedProcesses "run-no-mod-idle-layout-loop"
    if ($existing) {
        return
    }
    $heartbeat = Read-JsonFile (Join-Path $runtimeDir "idle-layout-loop.json")
    if ($heartbeat -and (Test-PidAlive $heartbeat.pid)) {
        return
    }
    Start-ManagedPython "unattended-idle-layout-loop" @(
        "-m", "factorio_ai.cli", "run-no-mod-idle-layout-loop",
        "--objective", $Objective,
        "--cycles", "0",
        "--sleep-seconds", "5",
        "--stale-seconds", [string]$IdleLoopStaleSeconds,
        "--min-submit-interval-seconds", "0"
    ) | Out-Null
}

function Write-FoundryWaitingHeartbeat {
    $payload = [ordered]@{
        pid = $null
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
        objective = $Objective
        state = "waiting_for_scheduler_llm"
        reason = "waiting for scheduler local LLM readiness"
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runtimeDir "skill-foundry-loop.json") -Encoding UTF8
}

function Ensure-SkillFoundryLoop {
    if ($env:FACTORIO_AI_SKILL_FOUNDRY_ENABLED -in @("0", "false", "False", "FALSE", "no", "off")) {
        Stop-ManagedProcesses "skill-foundry-loop" "run-no-mod-skill-foundry-loop"
        Write-FoundryWaitingHeartbeat
        return
    }
    if (-not (Test-SchedulerLlmReady)) {
        Stop-ManagedProcesses "skill-foundry-loop" "run-no-mod-skill-foundry-loop"
        Write-FoundryWaitingHeartbeat
        return
    }
    $processes = @(Get-ManagedProcesses "run-no-mod-skill-foundry-loop")
    $heartbeat = Read-JsonFile (Join-Path $runtimeDir "skill-foundry-loop.json")
    $ageSeconds = Get-JsonAgeSeconds $heartbeat
    $staleState = $false
    if ($processes.Count -eq 0) {
        $staleState = $true
    } elseif ($null -eq $heartbeat) {
        $staleState = $true
    } elseif ($heartbeat.state -in @("stopped", "failed", "interrupted")) {
        $staleState = $true
    } elseif ($ageSeconds -gt $FoundryStaleSeconds) {
        $staleState = $true
    }

    if (-not $staleState) {
        return
    }

    if ($processes.Count -gt 0) {
        Write-SupervisorLog "restarting stale skill foundry loop process"
        foreach ($process in $processes) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 3
    }
    Start-ManagedPython "unattended-skill-foundry-loop" @(
        "-m", "factorio_ai.cli", "run-no-mod-skill-foundry-loop",
        "--objective", $Objective,
        "--cycles", "0",
        "--sleep-seconds", [string]$FoundrySleepSeconds,
        "--require-idle",
        "--max-attempts", "3"
    ) | Out-Null
}

function Test-LiveSkillFresh {
    $live = Read-JsonFile (Join-Path $runtimeDir "live-skill-heartbeat.json")
    if ($null -eq $live -or $live.active -ne $true) {
        return $false
    }
    if ($live.pid -and -not (Test-PidAlive $live.pid)) {
        return $false
    }
    return (Get-JsonAgeSeconds $live) -lt ([Math]::Max($AutopilotStaleSeconds, 900))
}

function Start-Autopilot {
    param([bool]$RequireLlm = $true)
    if ($Driver -eq "code-agent") {
        # FLE-style driver: the LLM writes a Python program each step via the high-level API.
        Start-ManagedPython "unattended-no-mod-code-agent" @(
            "-m", "factorio_ai.cli", "run-no-mod-code-agent",
            "--objective", $Objective,
            "--cycles", "0",
            "--sleep-seconds", [string]$AutopilotSleepSeconds
        ) | Out-Null
    } else {
        $arguments = @(
            "-m", "factorio_ai.cli", "run-no-mod-autopilot",
            "--objective", $Objective
        )
        $processName = "unattended-no-mod-autopilot"
        if ($RequireLlm) {
            $arguments += "--require-llm"
        } else {
            $processName = "unattended-no-mod-autopilot-heuristic"
        }
        $arguments += @(
            "--cycles", "0",
            "--sleep-seconds", [string]$AutopilotSleepSeconds
        )
        $previousRequireLlm = $env:FACTORIO_AI_REQUIRE_LLM_STRATEGY
        $previousForceHeuristic = $env:FACTORIO_AI_FORCE_HEURISTIC_STRATEGY
        try {
            if ($RequireLlm) {
                $env:FACTORIO_AI_REQUIRE_LLM_STRATEGY = "1"
                $env:FACTORIO_AI_FORCE_HEURISTIC_STRATEGY = "0"
            } else {
                $env:FACTORIO_AI_REQUIRE_LLM_STRATEGY = "0"
                $env:FACTORIO_AI_FORCE_HEURISTIC_STRATEGY = "1"
            }
            Start-ManagedPython $processName $arguments | Out-Null
        } finally {
            $env:FACTORIO_AI_REQUIRE_LLM_STRATEGY = $previousRequireLlm
            $env:FACTORIO_AI_FORCE_HEURISTIC_STRATEGY = $previousForceHeuristic
        }
    }
}

function Ensure-Autopilot {
    if (-not (Test-SchedulerLlmReady)) {
        if (Test-HeuristicAutopilotFallbackAllowed) {
            $processes = @(Get-ManagedProcesses $DriverCli)
            if ($processes.Count -eq 0) {
                Write-SupervisorLog "scheduler LLM not ready; starting heuristic autopilot fallback"
                Start-Autopilot $false
                return
            }
            if (Test-AutopilotActiveCycle -or (Test-LiveSkillFresh)) {
                return
            }
            $heartbeat = Read-JsonFile (Join-Path $runtimeDir $DriverHeartbeatName)
            $ageSeconds = Get-JsonAgeSeconds $heartbeat
            $staleState = $false
            if ($null -eq $heartbeat) {
                $staleState = $true
            } elseif ($heartbeat.state -in @("stopped", "failed", "interrupted", "cycle_error")) {
                $staleState = $true
            } elseif ($ageSeconds -gt $AutopilotStaleSeconds) {
                $staleState = $true
            }
            if ($staleState) {
                Write-SupervisorLog "restarting stale heuristic autopilot fallback while scheduler LLM is unavailable"
                foreach ($process in $processes) {
                    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
                }
                Start-Sleep -Seconds 3
                Start-Autopilot $false
            }
            return
        }
        if (Test-AutopilotActiveCycle) {
            return
        }
        Stop-ManagedProcesses "autopilot" $DriverCli
        Write-AutopilotWaitingHeartbeat
        return
    }
    $processes = @(Get-ManagedProcesses $DriverCli)
    if ($Driver -eq "autopilot" -and $processes.Count -gt 0) {
        $fallbackProcesses = @($processes | Where-Object { -not (Test-ProcessRequiresLlm $_) })
        if ($fallbackProcesses.Count -gt 0) {
            Write-SupervisorLog "scheduler LLM ready; replacing heuristic autopilot fallback with strict LLM autopilot"
            foreach ($process in $processes) {
                Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
            }
            Start-Sleep -Seconds 3
            Start-Autopilot $true
            return
        }
    }
    if ($processes.Count -eq 0) {
        Start-Autopilot $true
        return
    }

    $heartbeat = Read-JsonFile (Join-Path $runtimeDir $DriverHeartbeatName)
    $ageSeconds = Get-JsonAgeSeconds $heartbeat
    $staleState = $false
    if ($null -eq $heartbeat) {
        $staleState = $true
    } elseif ($heartbeat.state -in @("stopped", "failed", "interrupted", "cycle_error")) {
        $staleState = $true
    } elseif ($ageSeconds -gt $AutopilotStaleSeconds -and -not (Test-LiveSkillFresh)) {
        $staleState = $true
    }

    if (-not $staleState) {
        return
    }

    Write-SupervisorLog "restarting stale autopilot process"
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
    Start-Autopilot $true
}

function Write-SupervisorStatus {
    $autopilot = Read-JsonFile (Join-Path $runtimeDir $DriverHeartbeatName)
    $idle = Read-JsonFile (Join-Path $runtimeDir "idle-layout-loop.json")
    $live = Read-JsonFile (Join-Path $runtimeDir "live-skill-heartbeat.json")
    $foundry = Read-JsonFile (Join-Path $runtimeDir "skill-foundry-loop.json")
    $autopilotGate = "waiting_for_scheduler_llm"
    if (Test-SchedulerLlmReady) {
        $autopilotGate = "ready"
    } elseif (Test-AutopilotActiveCycle) {
        $autopilotGate = "active_cycle_waiting_for_scheduler_capacity"
    } elseif (Test-HeuristicAutopilotFallbackAllowed -and @((Get-ManagedProcesses $DriverCli)).Count -gt 0) {
        $autopilotGate = "heuristic_fallback_waiting_for_scheduler_llm"
    }
    $status = [ordered]@{
        state = "running"
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
        objective = $Objective
        supervisor_pid = $PID
        check_seconds = $CheckSeconds
        scheduler_check_seconds = $SchedulerCheckSeconds
        autopilot_sleep_seconds = $AutopilotSleepSeconds
        autopilot_gate = $autopilotGate
        autopilot_processes = @((Get-ManagedProcesses $DriverCli | ForEach-Object { $_.ProcessId }))
        idle_layout_processes = @((Get-ManagedProcesses "run-no-mod-idle-layout-loop" | ForEach-Object { $_.ProcessId }))
        skill_foundry_processes = @((Get-ManagedProcesses "run-no-mod-skill-foundry-loop" | ForEach-Object { $_.ProcessId }))
        vllm_service_status = $lastVllmServiceStatus
        scheduler_llm_status = $lastSchedulerStatus
        autopilot_heartbeat = $autopilot
        live_skill_heartbeat = $live
        idle_layout_heartbeat = $idle
        skill_foundry_heartbeat = $foundry
    }
    $status | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
}

Write-SupervisorLog "unattended local LLM supervisor started for $Objective"
while ($true) {
    try {
        Ensure-NoModServer
        Ensure-NoModServerSave
        Ensure-Dashboard
        Ensure-LayoutCapacityLimit
        Ensure-SchedulerLlm
        Ensure-Autopilot
        Ensure-IdleLayoutLoop
        Ensure-SkillFoundryLoop
        Write-SupervisorStatus
    } catch {
        Write-SupervisorLog "supervisor loop error: $($_.Exception.GetType().Name): $($_.Exception.Message)"
    }
    Start-Sleep -Seconds ([Math]::Max(5, $CheckSeconds))
}
