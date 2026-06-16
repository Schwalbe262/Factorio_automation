@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=src

echo [factorio-ai] Clean restart of the unattended no-custom-mod local LLM supervisor.
echo [factorio-ai] This stops the old supervisor + loops + Factorio server, then relaunches.

echo [factorio-ai] Saving the current world before stopping (best-effort)...
python -m factorio_ai.cli no-mod-server-save >nul 2>nul

echo [factorio-ai] Stopping the old supervisor (so it cannot respawn its loops)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$me=$PID; Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $me -and $_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*run_factorio_no_mod_unattended_llm.ps1*' } | ForEach-Object { Write-Host ('  stop supervisor pid=' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 2"

echo [factorio-ai] Stopping autopilot / idle-layout / skill-foundry loops, server, dashboard, and Factorio...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*factorio_ai.cli*' } | ForEach-Object { Write-Host ('  stop python pid=' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-Process factorio -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ('  stop factorio pid=' + $_.Id); Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 3"

echo [factorio-ai] Relaunching unattended supervisor with the latest code...
call "%~dp0run_factorio_no_mod_unattended_llm.bat" %*

endlocal
