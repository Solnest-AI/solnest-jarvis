@echo off
REM Windows setup. Double-click this file, or run it in a terminal once.
cd /d "%~dp0"

REM Find Python. Prefer the py launcher (installed by python.org regardless of the
REM "Add Python to PATH" checkbox); fall back to bare python. Bare python alone can
REM hit the Microsoft Store stub or be missing from PATH.
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY ( python --version >nul 2>nul && set "PY=python" )
if not defined PY (
  echo.
  echo Python isn't installed ^(or isn't on your PATH^).
  echo Install it from:  https://www.python.org/downloads/
  echo During install, tick "Add python.exe to PATH", then run setup.bat again.
  echo.
  pause
  exit /b 1
)

echo Creating a private Python environment (.venv)...
%PY% -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip >nul
echo Installing dependencies (this can take a couple minutes)...
.venv\Scripts\pip install -r requirements.txt

echo.
echo Let's configure JARVIS...
.venv\Scripts\python configure.py

echo.
echo Setup done.
echo   1) Start JARVIS:  run.bat
echo   2) Open:          http://localhost:8800   (use Chrome)
pause
