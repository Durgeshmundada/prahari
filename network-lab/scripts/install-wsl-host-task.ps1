param(
    [string]$Distribution = "Ubuntu-24.04"
)
$ErrorActionPreference = "Stop"
$taskName = "PRAHARI WSL Keeper"
$startScript = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "start-wsl-host.ps1")).Path
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument (
        "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass " +
        "-File `"$startScript`" -Distribution `"$Distribution`""
    )
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Keeps the PRAHARI Ubuntu WSL host alive so user systemd recovery timers run." `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output "Installed and started scheduled task '$taskName'."
