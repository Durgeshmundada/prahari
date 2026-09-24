$ErrorActionPreference = "Stop"
$pidFile = Join-Path $PSScriptRoot "../state/host/wsl-keeper.pid"
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output "PRAHARI WSL host is not recorded as running."
    exit 0
}
$keeperPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $keeperPid" -ErrorAction SilentlyContinue
if ($null -eq $process -or $process.Name -ne "wsl.exe" -or $process.CommandLine -notlike "*wsl-keeper.sh*") {
    Remove-Item -LiteralPath $pidFile -Force
    throw "Recorded PID does not identify the PRAHARI WSL keeper; no process was stopped."
}
Stop-Process -Id $keeperPid
Remove-Item -LiteralPath $pidFile -Force
Write-Output "Stopped PRAHARI WSL host PID $keeperPid."
