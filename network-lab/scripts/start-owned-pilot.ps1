param(
    [string]$Distribution = "Ubuntu-24.04",
    [string]$StateDirectory = "/home/hp/.local/share/prahari-owned-pilot"
)
$ErrorActionPreference = "Stop"
if ($StateDirectory -notmatch '^/[a-zA-Z0-9/._-]+$') { throw "Unsupported pilot state path" }
$repoDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "../..")).Path
if ($repoDirectory -notmatch '^([a-zA-Z]):\\[a-zA-Z0-9\\._-]+$') { throw "Unsupported repository path" }
$driveLetter = $repoDirectory.Substring(0, 1).ToLowerInvariant()
$linuxRepository = "/mnt/$driveLetter/" + $repoDirectory.Substring(3).Replace('\', '/')
$hostState = Join-Path $repoDirectory "artifacts/owned-pilot-host"
New-Item -ItemType Directory -Force -Path $hostState | Out-Null
$pidFile = Join-Path $hostState "supervisor.pid"
if (Test-Path -LiteralPath $pidFile) {
    $existingPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $existingPid" -ErrorAction SilentlyContinue
    if ($null -ne $existing -and $existing.Name -eq "wsl.exe" -and $existing.CommandLine -like "*$StateDirectory/runtime/network-lab/scripts/run-owned-pilot.sh*") {
        Write-Output "Owned pilot supervisor already running (PID $existingPid)."
        exit 0
    }
}
$process = Start-Process -FilePath "wsl.exe" -ArgumentList @(
    "-d", $Distribution, "-u", "root", "--", "bash",
    "$StateDirectory/runtime/network-lab/scripts/run-owned-pilot.sh", $StateDirectory
) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $hostState "supervisor.log") `
  -RedirectStandardError (Join-Path $hostState "supervisor-error.log")
Set-Content -LiteralPath $pidFile -Value $process.Id -NoNewline
Write-Output "Owned pilot supervisor started (PID $($process.Id)); credentials remain in the Linux state directory."
