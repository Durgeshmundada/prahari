@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

echo.
echo SAFE MODE: this changes PRAHARI simulation data only.
echo It does not modify Wi-Fi, routing, firewall, latency, or packet loss.
echo.

uv run python scripts\simulate_dashboard_fault.py --scenario %~1
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
  echo Open or refresh http://127.0.0.1:5173
) else (
  echo The scenario was not loaded. Review the error above.
)
pause
exit /b %RESULT%
