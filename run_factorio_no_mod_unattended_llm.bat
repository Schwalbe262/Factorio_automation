@echo off
setlocal
cd /d "%~dp0"

echo [factorio-ai] Starting unattended no-custom-mod local LLM supervisor.
echo [factorio-ai] This keeps the server, dashboard, scheduler Qwen path, autopilot, and idle layout loop alive.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_factorio_no_mod_unattended_llm.ps1" %*
