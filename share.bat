@echo off
REM Reach JARVIS from your phone, anywhere, over your private Tailscale network.
REM Do this AFTER JARVIS is running (run.bat) and Tailscale is installed + signed in
REM on BOTH this PC and your phone (same account).
cd /d "%~dp0"
if "%JARVIS_PORT%"=="" set JARVIS_PORT=8800
where tailscale >nul 2>nul
if errorlevel 1 ( echo Tailscale isn't installed. Get it at https://tailscale.com/download, sign in, then run this again. & pause & exit /b 1 )
echo Publishing http://localhost:%JARVIS_PORT% to your private Tailscale network...
tailscale serve --bg --https=443 http://127.0.0.1:%JARVIS_PORT%
echo.
echo Live. Open this on your phone (Chrome, same Tailscale account):
tailscale serve status
echo To stop sharing later:  tailscale serve --https=443 off
pause
