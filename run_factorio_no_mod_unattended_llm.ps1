param(
    [string]$Objective = "launch_rocket_program",
    [int]$CheckSeconds = 30,
    [int]$SchedulerCheckSeconds = 60,
    [int]$AutopilotStaleSeconds = 900,
    [int]$AutopilotSleepSeconds = 60,
    [int]$IdleLoopStaleSeconds = 180,
    [int]$LayoutMaxActiveTasks = 1,
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
$env:FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL = "a6000ada,a6000,rtx3090"
$env:FACTORIO_AI_SLURM_SCHEDULER_PRIORITY = "100"
$env:FACTORIO_AI_SLURM_LAYOUT_GPU_MODELS = "a6000ada,a6000"
$env:FACTORIO_AI_SLURM_LAYOUT_CPUS = "3"
$env:FACTORIO_AI_SLURM_LAYOUT_PRIORITY = "80"
$env:FACTORIO_AI_VLLM_MODEL = "Qwen/Qwen3.5-9B"
$env:FACTORIO_AI_VLLM_ARGS = "--max-model-len 32768 --gpu-memory-utilization 0.90 --enforce-eager"
$env:FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER = "0"
$env:FACTORIO_AI_VLLM_PORT = "8000"
$env:FACTORIO_AI_VLLM_STARTUP_SECONDS = "420"
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

$runtimeDir = Join-Path $repoRoot "runtime"
$logDir = Join-Path $repoRoot "logs"
New-Item -ItemType Directory -Force -Path $runtimeDir, $logDir | Out-Null

$supervisorLog = Join-Path $logDir "unattended-llm-supervisor.log"
$statusPath = Join-Path $runtimeDir "unattended-llm-supervisor.json"
$lastSchedulerCheck = [DateTime]::MinValue
$lastSchedulerStatus = $null

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

function Ensure-NoModServer {
    $observeExit = Invoke-Cli @("-m", "factorio_ai.cli", "no-mod-observe")
    if ($observeExit -eq 0) {
        return
    }
    Write-SupervisorLog "no-mod RCON observe failed; ensuring save and starting server"
    Invoke-Cli @("-m", "factorio_ai.cli", "create-no-mod-save") | Out-Null
    Start-ManagedPython "unattended-no-mod-server" @("-m", "factorio_ai.cli", "start-no-mod-server") | Out-Null
    Start-Sleep -Seconds 12
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
    return $null -ne $script:lastSchedulerStatus -and $script:lastSchedulerStatus.llm_ready -eq $true
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
        scheduler_llm_status = $lastSchedulerStatus
        autopilot_heartbeat = $autopilot
        live_skill_heartbeat = $live
        idle_layout_heartbeat = $idle
    }
    $status | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
}

Write-SupervisorLog "unattended local LLM supervisor started for $Objective"
while ($true) {
    try {
        Ensure-NoModServer
        Ensure-Dashboard
        Ensure-LayoutCapacityLimit
        Ensure-SchedulerLlm
        Ensure-Autopilot
        Ensure-IdleLayoutLoop
        Write-SupervisorStatus
    } catch {
        Write-SupervisorLog "supervisor loop error: $($_.Exception.GetType().Name): $($_.Exception.Message)"
    }
    Start-Sleep -Seconds ([Math]::Max(5, $CheckSeconds))
}
