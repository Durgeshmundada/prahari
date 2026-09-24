@echo off
setlocal EnableExtensions

rem PRAHARI two-laptop demo launcher for Windows Command Prompt.
rem Double-click this file or run it from cmd.exe. An optional first argument
rem can provide the exact private IPv4 address to bind.

set "ROOT=%~dp0"
set "CHECK_ONLY=0"
set "DEMO_IP="

if /i "%~1"=="--check" (
  set "CHECK_ONLY=1"
) else if not "%~1"=="" (
  set "DEMO_IP=%~1"
)
if /i "%~2"=="--check" set "CHECK_ONLY=1"

cd /d "%ROOT%"

where uv >nul 2>&1
if errorlevel 1 (
  echo ERROR: uv was not found. Install uv or add it to PATH, then run this file again.
  pause
  exit /b 1
)

where npm >nul 2>&1
if errorlevel 1 (
  echo ERROR: npm was not found. Install Node.js 22 or newer, then run this file again.
  pause
  exit /b 1
)

if not exist "%ROOT%scripts\traffic_demo_user_service.py" (
  echo ERROR: Run this launcher from the PRAHARI repository.
  pause
  exit /b 1
)

if not defined DEMO_IP (
  for /f "usebackq delims=" %%I in (`uv run python -c "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('192.0.2.1',9)); print(s.getsockname()[0]); s.close()"`) do if not defined DEMO_IP set "DEMO_IP=%%I"
)

if not defined DEMO_IP (
  echo ERROR: No active private IPv4 address with a default gateway was found.
  echo Connect Laptop A and Laptop B to the same phone hotspot, then run this file again.
  pause
  exit /b 1
)

for /f "usebackq delims=" %%T in (`uv run python -c "import secrets; print(secrets.token_hex(24))"`) do set "PRAHARI_USER_DEMO_TOKEN=%%T"
if not defined PRAHARI_USER_DEMO_TOKEN (
  echo ERROR: A temporary access code could not be generated.
  pause
  exit /b 1
)

echo.
echo PRAHARI two-laptop demo
echo -----------------------
echo Laptop A address : %DEMO_IP%
echo Laptop B URL     : http://%DEMO_IP%:8780
echo Access code      : %PRAHARI_USER_DEMO_TOKEN%
echo.
echo Keep this window visible so you can copy the URL and access code.
echo Uploaded content and chat text are not stored by the demo service.
echo.

if "%CHECK_ONLY%"=="1" (
  echo Launcher check passed. No services were started.
  exit /b 0
)

echo Preparing the local database...
uv run alembic upgrade head
if errorlevel 1 (
  echo ERROR: Database preparation failed. Review the message above.
  pause
  exit /b 1
)

if not exist "%ROOT%apps\dashboard\node_modules" (
  echo Installing dashboard dependencies once...
  pushd "%ROOT%apps\dashboard"
  call npm ci
  if errorlevel 1 (
    popd
    echo ERROR: Dashboard dependency installation failed.
    pause
    exit /b 1
  )
  popd
)

curl.exe --silent --fail --max-time 2 http://127.0.0.1:8000/health >nul 2>&1
if errorlevel 1 (
  echo Starting the PRAHARI API in a separate window...
  start "PRAHARI API" /D "%ROOT%" cmd /k "uv run uvicorn prahari.main:app --app-dir src --host 127.0.0.1 --port 8000"
) else (
  echo The PRAHARI API is already running on port 8000.
)

curl.exe --silent --fail --max-time 2 http://127.0.0.1:5173/ >nul 2>&1
if errorlevel 1 (
  echo Starting the PRAHARI dashboard in a separate window...
  start "PRAHARI Dashboard" /D "%ROOT%apps\dashboard" cmd /k "npm run dev -- --host 127.0.0.1"
) else (
  echo The PRAHARI dashboard is already running on port 5173.
)

echo Opening the operator dashboard on Laptop A...
powershell -NoProfile -Command "Start-Sleep -Seconds 3" >nul
start "" "http://127.0.0.1:5173"

echo Starting the Laptop B user service in this window...
echo If Windows Firewall asks, allow access only on Private networks.
echo Press Ctrl+C to stop the user service.
echo.

uv run python scripts\traffic_demo_user_service.py --host %DEMO_IP% --allow-non-loopback

echo.
echo The user service has stopped. Close the API and Dashboard windows when finished.
pause
