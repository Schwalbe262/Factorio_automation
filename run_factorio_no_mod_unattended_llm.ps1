param(
    [string]$Objective = "launch_rocket_program",
    [int]$CheckSeconds = 30,
    [int]$SchedulerCheckSeconds = 60,
    [int]$AutopilotStaleSeconds = 900,
    [int]$AutopilotSleepSeconds = 60,
    [int]$IdleLoopStaleSeconds = 180,
    [int]$LayoutMaxActiveTasks = 1,
    [int]$FoundryStaleSeconds = 900,
    [int]$FoundrySleepSeconds = 20,
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
$env:FACTORIO_AI_SLURM_REMOTE_DIR = "~/factorio-ai-worker"
$env:FACTORIO_AI_SLURM_TASK_TIMEOUT_SECONDS = "900"
$env:FACTORIO_AI_SLURM_SCHEDULER_CPUS = "3"
$env:FACTORIO_AI_SLURM_SCHEDULER_GPUS = "1"
$env:FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL = "a6000ada,a6000"
$env:FACTORIO_AI_SLURM_SCHEDULER_PRIORITY = "100"
$env:FACTORIO_AI_SLURM_LAYOUT_GPU_MODELS = "a6000ada,a6000"
$env:FACTORIO_AI_SLURM_LAYOUT_CPUS = "3"
$env:FACTORIO_AI_SLURM_LAYOUT_PRIORITY = "80"
# Code-specialized 32B at 4-bit AWQ (~18GB) fits one A6000; far better at writing skill code
# than the 9B (which could not produce passing skills). Two instances (one per GPU) below.
$env:FACTORIO_AI_VLLM_MODEL = "Qwen/Qwen2.5-Coder-32B-Instruct-AWQ"
$env:FACTORIO_AI_VLLM_ARGS = "--max-model-len 32768 --gpu-memory-utilization 0.90 --quantization awq --enforce-eager"
$env:FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER = "0"
$env:FACTORIO_AI_VLLM_PORT = "8000"
$env:FACTORIO_AI_VLLM_STARTUP_SECONDS = "420"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_ENABLED = "1"
# Run 2 warm 32B-AWQ instances (each on its own GPU/node) for parallel throughput +
# redundancy: client tasks round-robin across them and one can serve while the
# other cold-loads or restarts. All share port 8000 (each on its own node).
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_COUNT = "2"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_DURATION_SECONDS = "43200"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_HEARTBEAT_SECONDS = "30"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_STALE_SECONDS = "120"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_QUEUE_STALE_SECONDS = "180"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_CPUS = "1"
$env:FACTORIO_AI_SCHEDULER_VLLM_CLIENT_CPUS = "1"
$env:FACTORIO_AI_SCHEDULER_VLLM_CLIENT_GPUS = "0"
$env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_PRIORITY = "120"
$env:FACTORIO_AI_REQUIRE_LLM_STRATEGY = "1"
$env:FACTORIO_AI_LLM_GUIDED_JSON = "1"
$env:FACTORIO_AI_LLM_TIMEOUT = "600"
$env:FACTORIO_AI_REMOTE_STRATEGY_TIMEOUT_SECONDS = "900"
$env:FACTORIO_AI_BACKGROUND_LAYOUT_ENABLED = "1"
$env:FACTORIO_AI_BACKGROUND_LAYOUT_MODE = "scheduler"
$env:FACTORIO_AI_BACKGROUND_LAYOUT_MAX_ACTIVE_TASKS = [string]$LayoutMaxActiveTasks
$env:FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS = "0"
$env:FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES = "360"
$env:FACTORIO_AI_SLURM_RENEW_CHECK_INTERVAL_SECONDS = "1800"
# Self-development (skill foundry): the local Qwen authors missing executors and each
# candidate is exercised on a throwaway COPY of the live save before it can run live.
$env:FACTORIO_AI_FOUNDRY_SANDBOX_ENABLED = "1"
$env:FACTORIO_AI_FOUNDRY_SANDBOX_SERVER_PORT = "34297"
$env:FACTORIO_AI_FOUNDRY_SANDBOX_RCON_PORT = "27115"
$env:FACTORIO_AI_FOUNDRY_SANDBOX_RCON_TIMEOUT = "180"
$env:FACTORIO_AI_FOUNDRY_MAX_ATTEMPTS = "3"
$env:FACTORIO_AI_FOUNDRY_MAX_TOKENS = "3072"
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
    $strict = $true
    if ($null -eq $script:lastSchedulerStatus -or $script:lastSchedulerStatus.llm_ready -ne $true) {
        $strict = $false
    }
    elseif ($env:FACTORIO_AI_SCHEDULER_VLLM_SERVICE_ENABLED -notin @("0", "false", "False", "FALSE", "no", "off")) {
        if (-not ($null -ne $script:lastVllmServiceStatus -and $script:lastVllmServiceStatus.service_ready -eq $true)) {
            $strict = $false
        }
    }
    if ($strict) {
        $script:lastLlmReadyAt = Get-Date
        return $true
    }
    # Ride through transient scheduler-API blips: if the LLM was confirmed ready very recently, keep the
    # loops alive instead of tearing them down on a single failed/timed-out status check.
    if ($script:lastLlmReadyAt -ne [DateTime]::MinValue -and ((Get-Date) - $script:lastLlmReadyAt).TotalSeconds -lt $LlmReadyGraceSeconds) {
        return $true
    }
    return $false
}

function Test-AutopilotActiveCycle {
    $heartbeat = Read-JsonFile (Join-Path $runtimeDir "autopilot-heartbeat.json")
    if ($null -eq $heartbeat -or $heartbeat.state -ne "cycle_start") {
        return $false
    }
    if ($heartbeat.pid -and -not (Test-PidAlive $heartbeat.pid)) {
        return $false
    }
    return (Get-JsonAgeSeconds $heartbeat) -lt $AutopilotStaleSeconds
}

function Write-AutopilotWaitingHeartbeat {
    $existing = Read-JsonFile (Join-Path $runtimeDir "autopilot-heartbeat.json")
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
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runtimeDir "autopilot-heartbeat.json") -Encoding UTF8
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
    Start-ManagedPython "unattended-no-mod-autopilot" @(
        "-m", "factorio_ai.cli", "run-no-mod-autopilot",
        "--objective", $Objective,
        "--require-llm",
        "--cycles", "0",
        "--sleep-seconds", [string]$AutopilotSleepSeconds
    ) | Out-Null
}

function Ensure-Autopilot {
    if (-not (Test-SchedulerLlmReady)) {
        if (Test-AutopilotActiveCycle) {
            return
        }
        Stop-ManagedProcesses "autopilot" "run-no-mod-autopilot"
        Write-AutopilotWaitingHeartbeat
        return
    }
    $processes = @(Get-ManagedProcesses "run-no-mod-autopilot")
    if ($processes.Count -eq 0) {
        Start-Autopilot
        return
    }

    $heartbeat = Read-JsonFile (Join-Path $runtimeDir "autopilot-heartbeat.json")
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
    Start-Autopilot
}

function Write-SupervisorStatus {
    $autopilot = Read-JsonFile (Join-Path $runtimeDir "autopilot-heartbeat.json")
    $idle = Read-JsonFile (Join-Path $runtimeDir "idle-layout-loop.json")
    $live = Read-JsonFile (Join-Path $runtimeDir "live-skill-heartbeat.json")
    $foundry = Read-JsonFile (Join-Path $runtimeDir "skill-foundry-loop.json")
    $autopilotGate = "waiting_for_scheduler_llm"
    if (Test-SchedulerLlmReady) {
        $autopilotGate = "ready"
    } elseif (Test-AutopilotActiveCycle) {
        $autopilotGate = "active_cycle_waiting_for_scheduler_capacity"
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
        autopilot_processes = @((Get-ManagedProcesses "run-no-mod-autopilot" | ForEach-Object { $_.ProcessId }))
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
