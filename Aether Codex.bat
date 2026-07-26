@echo off
REM ============================================================
REM  Aether Codex launcher — double-click this file to start.
REM ============================================================
title Aether Codex
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

REM --- first-run checks --------------------------------------
if not exist "%PY%" (
  echo.
  echo   [!] Virtual environment not found ^(.venv^).
  echo       First-time setup, run these once in this folder:
  echo.
  echo         python -m venv .venv
  echo         .venv\Scripts\pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  echo.
  echo   [!] No .env file found. The app needs an API key to run.
  echo       Copy .env.example to .env and add your key, e.g.:
  echo.
  echo         copy .env.example .env
  echo         notepad .env
  echo.
  echo   You can still open the menu, but runs will fail until a key is set.
  echo.
  pause
)

:menu
cls
echo.
echo   ==========================================================
echo             AETHER CODEX  -  multi-agent research
echo   ==========================================================
echo.
echo     [1]  Start web app        (opens in your browser)
echo     [2]  Terminal chat        (no browser)
echo     [3]  Fuzz a repo          (long invariant campaign)
echo     [4]  Open reports folder
echo     [5]  Quit
echo.
set "choice="
set /p "choice=  Choose an option [1-5]: "

if "%choice%"=="1" goto web
if "%choice%"=="2" goto cli
if "%choice%"=="3" goto fuzz
if "%choice%"=="4" goto reports
if "%choice%"=="5" goto end
echo   Invalid choice.
timeout /t 1 >nul
goto menu

:web
echo.
echo   Starting web app... a browser tab will open at http://127.0.0.1:7860
echo   Keep this window open while you use it. Press Ctrl+C here to stop.
echo.
set "AETHER_OPEN_BROWSER=1"
"%PY%" app.py
set "AETHER_OPEN_BROWSER="
echo.
echo   Web app stopped.
pause
goto menu

:cli
echo.
echo   Starting terminal chat. Type 'exit' to return to this menu.
echo.
"%PY%" -m aether_codex.cli
pause
goto menu

:fuzz
echo.
echo   FUZZ RIG - point it at a repo you dropped in audit_target\ and walk away.
echo   It plans invariants, then grinds forge for hours, and reports any breaks.
echo.
set "sub="
set /p "sub=  Subfolder under audit_target\ (blank = all): "
set "hrs="
set /p "hrs=  Hours to fuzz [default 2]: "
if "%hrs%"=="" set "hrs=2"
echo.
echo   Starting campaign (Ctrl+C here to stop early)...
echo.
"%PY%" -m aether_codex.fuzz_rig %sub% --hours %hrs%
echo.
pause
goto menu

:reports
if not exist "reports" mkdir "reports"
start "" "reports"
goto menu

:end
echo.
echo   Goodbye.
timeout /t 1 >nul
exit /b 0
