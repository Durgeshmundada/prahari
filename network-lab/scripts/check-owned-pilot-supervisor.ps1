param(
    [string]$PilotTaskName = "PRAHARI-Owned-Pilot",
    [string]$StateDirectory = "/home/hp/.local/share/prahari-owned-pilot"
)

$ErrorActionPreference = "Stop"
if ($PilotTaskName -notmatch '^[a-zA-Z0-9._-]+$' -or $StateDirectory -notmatch '^/[a-zA-Z0-9/._-]+$') {
    throw "Unsupported owned pilot task name or state directory"
}

$repoDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "../..")).Path
$hostRunner = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "run-owned-pilot-host.ps1")).Path
$reportDirectory = Join-Path $repoDirectory "artifacts/owned-pilot-host"
New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
if ((Get-Item -LiteralPath $reportDirectory).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Supervisor report directory must not be a reparse point"
}

$task = Get-ScheduledTask -TaskName $PilotTaskName -ErrorAction Stop
if (
    $task.Actions.Count -ne 1 -or
    $task.Actions[0].Execute -notmatch '(?i)(^|\\)powershell\.exe$' -or
    -not $task.Actions[0].Arguments.Contains($hostRunner) -or
    -not $task.Actions[0].Arguments.Contains($StateDirectory)
) {
    throw "The owned pilot task action does not match the reviewed host runner"
}

$stateBefore = [string]$task.State
$started = $false
if ($stateBefore -ne "Running") {
    Start-ScheduledTask -TaskName $PilotTaskName
    $started = $true
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        $task = Get-ScheduledTask -TaskName $PilotTaskName -ErrorAction Stop
    } while ([string]$task.State -ne "Running" -and [DateTimeOffset]::UtcNow -lt $deadline)
}

$stateAfter = [string]$task.State
$taskInfo = Get-ScheduledTaskInfo -TaskName $PilotTaskName
$report = [ordered]@{
    schema_version = "owned-pilot-supervisor-watchdog-v1"
    checked_at = [DateTimeOffset]::UtcNow.ToString("o")
    checker_sha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
    pilot_task_name = $PilotTaskName
    state_directory = $StateDirectory
    state_before = $stateBefore
    state_after = $stateAfter
    start_requested = $started
    last_run_time = $taskInfo.LastRunTime.ToUniversalTime().ToString("o")
    last_task_result = [uint32]$taskInfo.LastTaskResult
    healthy = $stateAfter -eq "Running"
}
$output = Join-Path $reportDirectory "supervisor-watchdog.json"
$temporary = "$output.tmp.$PID"
try {
    $report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $output -Force
} finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
}
if (-not $report.healthy) {
    Write-Error "Owned pilot keeper task is not running after the watchdog check"
    exit 2
}

