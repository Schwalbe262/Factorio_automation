@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=src

echo [factorio-ai] Stopping the current no-custom-mod server if it is running...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*factorio_ai.cli start-no-mod-server*' -or ($_.Name -eq 'factorio.exe' -and $_.CommandLine -like '*no-mod-rcon.zip*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [factorio-ai] Creating a fresh no-custom-mod map with enlarged safe starting area...
python -m factorio_ai.cli create-no-mod-save --overwrite || exit /b 1

echo [factorio-ai] Done. Run run_factorio_no_mod_autopilot.bat or run_factorio_watch_gui.bat next.
