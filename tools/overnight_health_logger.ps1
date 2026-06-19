# Lightweight overnight health trail -> runtime/overnight-health.log
# Appends one compact line every 5 min from LOCAL files only (no SSH), so it is cheap and
# cannot disturb the cluster. Launch detached; it self-stops after ~12h.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$log = Join-Path $repo "runtime\overnight-health.log"
$sup = Join-Path $repo "runtime\unattended-llm-supervisor.json"
$traces = Join-Path $repo "logs\llm_io_traces.jsonl"
$deadline = (Get-Date).AddHours(12)
Add-Content -Path $log -Value ("=== health logger (re)started {0} ===" -f (Get-Date -Format o)) -Encoding UTF8
while ((Get-Date) -lt $deadline) {
    $stamp = (Get-Date -Format "HH:mm:ss")
    $gate = "?"; $svcReady = "?"
    try {
        $j = Get-Content -LiteralPath $sup -Raw -Encoding UTF8 | ConvertFrom-Json
        $gate = $j.autopilot_gate
        if ($j.vllm_service_status) { $svcReady = $j.vllm_service_status.service_ready }
    } catch {}
    $tcount = 0; $last = ""
    try {
        $lines = Get-Content -LiteralPath $traces -ErrorAction Stop
        $tcount = $lines.Count
        if ($tcount -gt 0) {
            $r = $lines[-1] | ConvertFrom-Json
            $last = "ok=$($r.ok),kind=$($r.kind),dur_ms=$($r.duration_ms)"
        }
    } catch {}
    Add-Content -Path $log -Value ("[{0}] gate={1} svc_ready={2} traces={3} last={4}" -f $stamp, $gate, $svcReady, $tcount, $last) -Encoding UTF8
    Start-Sleep -Seconds 300
}
Add-Content -Path $log -Value ("=== health logger exited {0} ===" -f (Get-Date -Format o)) -Encoding UTF8
