@echo off
setlocal
cd /d "%~dp0"

echo [factorio-ai] Starting unattended supervisor with the FLE code-generation driver.
echo [factorio-ai] The LLM writes a Python program each step (run-no-mod-code-agent) instead of the
echo [factorio-ai] deterministic strategy autopilot. Server, dashboard, serving, foundry, layout are
echo [factorio-ai] the same as the normal launcher; only the game-driver changes.
set "FACTORIO_AI_DRIVER=code-agent"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_factorio_no_mod_unattended_llm.ps1" %*
