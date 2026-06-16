@echo off
rem One-click FULL reset of the unattended no-custom-mod local LLM run.
rem Saves the world, stops the old supervisor + loops + server + dashboard + Factorio,
rem ALSO cancels the remote scheduler vLLM service(s), then relaunches the supervisor
rem (which loads a FRESH vLLM service -- slow, ~minutes for the 9B model).
rem
rem For a normal FAST restart that reuses the already-warm vLLM service, use
rem   restart_factorio_no_mod_unattended_llm.bat
rem instead. Only use this full reset when the vLLM service looks stuck or piled up.
setlocal
cd /d "%~dp0"
call "%~dp0restart_factorio_no_mod_unattended_llm.bat" reset-vllm
endlocal
