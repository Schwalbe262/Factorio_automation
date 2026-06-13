@echo off
setlocal
cd /d "%~dp0"
set PYTHONPATH=src

echo [factorio-ai] Probing vanilla Factorio window capture/input capabilities...
echo [factorio-ai] Keep Factorio open, but do not minimize it for automation.
echo [factorio-ai] This uses no mods, no RCON, and no Lua commands.
python -m factorio_ai.cli vanilla-window
python -m factorio_ai.cli vanilla-screenshot --output runtime\vanilla\screenshots\current.bmp --method auto
python -m factorio_ai.cli vanilla-screen-state --output runtime\vanilla\screenshots\screen-state.bmp --method auto
python -m factorio_ai.cli vanilla-probe --minimize-check --output-dir runtime\vanilla\probe
if errorlevel 1 (
  echo [factorio-ai] Vanilla probe failed. Check the printed error above.
  pause
  exit /b 1
)

echo [factorio-ai] Probe complete.
echo [factorio-ai] Screenshots are under runtime\vanilla.
