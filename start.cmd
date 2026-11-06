@echo off
rem One command to start the setup interview: double-click this file, or in a terminal opened in this folder run  .\start.cmd  (PowerShell) or  start.cmd  (Command Prompt).
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "questions.json" goto :broken
if not exist ".claude\skills\setup\SKILL.md" goto :broken

set "PY="
call :findpython
if defined PY goto :havepython

where winget >nul 2>nul || goto :nopython
echo Python 3.10 or newer is needed and was not found on this computer.
choice /C YN /M "Install Python 3.12 now with winget (about 100 MB)"
if errorlevel 2 goto :nopython
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent
call :findpython
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY goto :restart

:havepython
where claude >nul 2>nul || goto :noclaude
call %PY% finish_setup.py --trust-only "%~dp0." >nul 2>nul
echo Starting the setup interview. Answer the questions; say ok to accept a suggestion.
claude "set me up"
goto :eof

:findpython
where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
if defined PY exit /b 0
where py >nul 2>nul && call py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
exit /b 0

:nopython
echo.
echo Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run start.cmd again.
goto :fail

:noclaude
echo.
echo Claude Code was not found. Install it from https://claude.com/claude-code, sign in once, then run start.cmd again.
goto :fail

:restart
echo.
echo Python was installed. Close this window and run start.cmd again so that it can be found.
goto :fail

:broken
echo.
echo This file has to stay in the kit folder, next to questions.json. If you opened it from inside a zip file, extract the whole folder first, then run start.cmd again.
goto :fail

:fail
echo.
pause
exit /b 1
