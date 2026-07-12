@echo off
REM Windows: start JARVIS. Double-click, or run in a terminal.
cd /d "%~dp0"
if not exist .venv ( echo Run setup.bat first. & pause & exit /b 1 )
.venv\Scripts\python server.py
pause
