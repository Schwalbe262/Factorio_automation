@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=src

echo [factorio-ai] Installing development mod...
python -m factorio_ai.cli install-mod || exit /b 1

echo [factorio-ai] Ensuring save exists...
python -m factorio_ai.cli create-save || exit /b 1

echo [factorio-ai] Starting headless-style local server in a separate window...
start "Factorio AI Server" cmd /k "cd /d ""%~dp0"" && set PYTHONPATH=src && python -m factorio_ai.cli start-server"

echo [factorio-ai] Waiting for RCON...
timeout /t 12 /nobreak >nul

:strategy_loop
echo [factorio-ai] Running one strategic step...
python -m factorio_ai.cli run-strategy-step --objective launch_rocket_program
if errorlevel 1 (
  echo [factorio-ai] Strategy step stopped. Check the printed reason and logs.
  exit /b 1
)
timeout /t 2 /nobreak >nul
goto strategy_loop
