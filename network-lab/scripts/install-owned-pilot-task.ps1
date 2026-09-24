param(
    [string]$Distribution = "Ubuntu-24.04",
    [string]$StateDirectory = "/home/hp/.local/share/prahari-owned-pilot",
    [ValidateRange(5, 1024)]
    [int]$MinimumHostFreeGiB = 20,
    [ValidateRange(5, 1024)]
    [int]$MinimumStateFreeGiB = 20
)
$ErrorActionPreference = "Stop"
if ($StateDirectory -notmatch '^/[a-zA-Z0-9/._-]+$' -or $Distribution -notmatch '^[a-zA-Z0-9._-]+$') {
    throw "Unsupported owned pilot path or distribution"
}
$scriptPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "run-owned-pilot-host.ps1")).Path
$capacityScriptPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "check-owned-pilot-capacity.ps1")).Path
$watchdogScriptPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "check-owned-pilot-supervisor.ps1")).Path
$taskName = "PRAHARI-Owned-Pilot"
$capacityTaskName = "PRAHARI-Owned-Pilot-Capacity"
$watchdogTaskName = "PRAHARI-Owned-Pilot-Watchdog"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existing -and ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Arguments -notlike "*$scriptPath*")) {
    throw "An unrelated scheduled task already uses the pilot name; refusing replacement"
}
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $scriptPath +
    '" -Distribution "' + $Distribution + '" -StateDirectory "' + $StateDirectory +
    '" -MinimumHostFreeGiB ' + $MinimumHostFreeGiB
)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew -Hidden
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
  -Settings $settings -Description "Supervise the isolated PRAHARI owned pilot; never change the original lab." -Force | Out-Null

$capacityAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $capacityScriptPath +
    '" -Distribution "' + $Distribution + '" -StateDirectory "' + $StateDirectory +
    '" -MinimumHostFreeGiB ' + $MinimumHostFreeGiB +
    ' -MinimumStateFreeGiB ' + $MinimumStateFreeGiB
)
$capacityTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$capacitySettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew -StartWhenAvailable -Hidden
$existingCapacity = Get-ScheduledTask -TaskName $capacityTaskName -ErrorAction SilentlyContinue
if ($null -ne $existingCapacity -and (
    $existingCapacity.Actions.Count -ne 1 -or $existingCapacity.Actions[0].Arguments -notlike "*$capacityScriptPath*"
)) {
    throw "An unrelated scheduled task already uses the pilot capacity name; refusing replacement"
}
Register-ScheduledTask -TaskName $capacityTaskName -Action $capacityAction -Trigger $capacityTrigger `
    -Principal $principal -Settings $capacitySettings `
    -Description "Measure WSL backing-drive and owned-pilot filesystem capacity every five minutes." `
    -Force | Out-Null
$watchdogAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' +
    $watchdogScriptPath + '" -PilotTaskName "' + $taskName + '" -StateDirectory "' +
    $StateDirectory + '"'
)
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$watchdogSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable -Hidden
$existingWatchdog = Get-ScheduledTask -TaskName $watchdogTaskName -ErrorAction SilentlyContinue
if ($null -ne $existingWatchdog -and (
    $existingWatchdog.Actions.Count -ne 1 -or
    $existingWatchdog.Actions[0].Arguments -notlike "*$watchdogScriptPath*"
)) {
    throw "An unrelated scheduled task already uses the pilot watchdog name; refusing replacement"
}
Register-ScheduledTask -TaskName $watchdogTaskName -Action $watchdogAction `
    -Trigger $watchdogTrigger -Principal $principal -Settings $watchdogSettings `
    -Description "Restart the owned-pilot WSL keeper within five minutes after an unexpected exit." `
    -Force | Out-Null
Start-ScheduledTask -TaskName $capacityTaskName
Start-ScheduledTask -TaskName $watchdogTaskName
Write-Output "Owned pilot, capacity, and keeper-watchdog tasks installed. Start supervision with Start-ScheduledTask -TaskName '$taskName'."
