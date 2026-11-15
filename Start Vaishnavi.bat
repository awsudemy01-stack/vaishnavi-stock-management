@echo off
rem Vaishnavi Store Manager - Windows start wrapper.
rem Double-click this file. It finds Python, then runs main.py which installs/checks/starts everything.
rem Nothing in this file needs editing. Documentation: docs\17-install-and-run.md

setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

rem Prefer a venv created by an earlier run (fast path), then the Python launcher, then python on PATH.
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY ( where python >nul 2>nul && set "PY=python" )

if not defined PY (
  echo.
  echo  Vaishnavi Store Manager needs Python 3.11 or newer, and no Python was found on this computer.
  echo.
  echo  How to fix:
  echo    1. Open the Microsoft Store and install "Python 3.12"  ^(or download from https://www.python.org/downloads/^)
  echo    2. If you use the python.org installer, tick "Add python.exe to PATH".
  echo    3. Double-click "Start Vaishnavi.bat" again.
  echo.
  echo  Error code: E-SYS-001
  echo.
  pause
  exit /b 2
)

%PY% main.py %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo  Setup ended with code %CODE%. The messages above say what to do; the full log is in logs\install\ .
  pause
)
exit /b %CODE%
