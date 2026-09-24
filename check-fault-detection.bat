@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
  echo ERROR: uv was not found in PATH.
  pause
  exit /b 1
)

uv run python scripts\check_fault_detection.py
set "CHECK_RESULT=%ERRORLEVEL%"
echo.
if not "%CHECK_RESULT%"=="0" echo Review the error above before presenting a fault-detection claim.
pause
exit /b %CHECK_RESULT%
