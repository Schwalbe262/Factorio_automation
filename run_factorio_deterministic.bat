@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
if not "%~1"=="" (
  python -m factorio_ai.deterministic_cli run-no-mod-deterministic %*
) else if exist "runtime\deterministic\20260908\vanilla\saves\no-mod-rcon.zip" (
  python -m factorio_ai.deterministic_cli run-no-mod-deterministic --resume
) else (
  python -m factorio_ai.deterministic_cli run-no-mod-deterministic --new-world
)
if errorlevel 1 pause
