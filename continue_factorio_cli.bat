@echo off
setlocal
cd /d "%~dp0"

set "HANDOFF=%CD%\HANDOFF_CURRENT.md"
set "PROMPT=Read HANDOFF_CURRENT.md first, then AGENTS.md and minimal project metadata only. Do not resume long old threads or read old handoffs/journals/logs in full. Verify current git status, use targeted source ranges, commit/push each completed part, then continue the next concrete step from the current handoff."

echo Factorio Automation CLI handoff
echo Workspace: %CD%
echo Handoff: %HANDOFF%
echo.
echo Starting Codex CLI with the handoff prompt...
echo.

codex "%PROMPT%"
if %ERRORLEVEL% EQU 0 goto :done

echo.
echo Codex CLI did not start from this shell.
echo Open a CLI manually in this folder and paste this prompt:
echo.
echo %PROMPT%
echo.
echo The handoff document will be opened now.
start "" "%HANDOFF%"
pause

:done
endlocal
