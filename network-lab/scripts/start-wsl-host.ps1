param(
    [string]$Distribution = "Ubuntu-24.04"
)
$ErrorActionPreference = "Stop"
$stateDirectory = Join-Path $PSScriptRoot "../state/host"
$pidFile = Join-Path $stateDirectory "wsl-keeper.pid"
New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
if (Test-Path -LiteralPath $pidFile) {
    $existingPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $existingPid" -ErrorAction SilentlyContinue
    if ($null -ne $existing -and $existing.Name -eq "wsl.exe" -and $existing.CommandLine -like "*wsl-keeper.sh*") {
        Write-Output "PRAHARI WSL host is already running with PID $existingPid."
        exit 0
    }
}
$process = Start-Process -FilePath "wsl.exe" -ArgumentList @(
    "-d", $Distribution, "--", "bash",
    "/mnt/d/hack2skill/network-lab/scripts/wsl-keeper.sh"
) -WindowStyle Hidden -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id -NoNewline
Write-Output "Started PRAHARI WSL host with PID $($process.Id)."
