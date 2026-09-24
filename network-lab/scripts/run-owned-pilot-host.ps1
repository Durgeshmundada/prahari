param(
    [string]$Distribution = "Ubuntu-24.04",
    [string]$StateDirectory = "/home/hp/.local/share/prahari-owned-pilot",
    [ValidateRange(5, 1024)]
    [int]$MinimumHostFreeGiB = 20
)
$ErrorActionPreference = "Stop"
if ($StateDirectory -notmatch '^/[a-zA-Z0-9/._-]+$' -or $Distribution -notmatch '^[a-zA-Z0-9._-]+$') {
    throw "Unsupported owned pilot path or distribution"
}
$repoDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "../..")).Path
$hostState = Join-Path $repoDirectory "artifacts/owned-pilot-host"
New-Item -ItemType Directory -Force -Path $hostState | Out-Null
$registration = Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' |
    ForEach-Object { Get-ItemProperty $_.PSPath } |
    Where-Object { $_.DistributionName -eq $Distribution }
if (@($registration).Count -ne 1) {
    throw "Cannot resolve one WSL registration for $Distribution"
}
$backingRoot = Split-Path -Qualifier $registration.BasePath
$backingDrive = Get-PSDrive -Name $backingRoot.TrimEnd(':') -PSProvider FileSystem
if ($backingDrive.Free -lt ([uint64]$MinimumHostFreeGiB * 1GB)) {
    throw "WSL backing drive $backingRoot has less than $MinimumHostFreeGiB GiB free; refusing pilot startup"
}
# Windows PowerShell turns native stderr into ErrorRecord objects. When the
# persistent Linux supervisor is installed, this Windows process only holds a
# foreground WSL client; systemd owns recovery and measurement inside Linux.
$ErrorActionPreference = "Continue"
& wsl.exe -d $Distribution -u root -- systemctl is-enabled prahari-owned-pilot.service `
  *>> (Join-Path $hostState "scheduled-supervisor.log")
$persistentSupervisor = $LASTEXITCODE -eq 0
if ($persistentSupervisor) {
    & wsl.exe -d $Distribution -u root -- bash "$StateDirectory/runtime/network-lab/scripts/wsl-keeper.sh" `
      *>> (Join-Path $hostState "scheduled-supervisor.log")
} else {
    & wsl.exe -d $Distribution -u root -- bash "$StateDirectory/runtime/network-lab/scripts/run-owned-pilot.sh" $StateDirectory `
      *>> (Join-Path $hostState "scheduled-supervisor.log")
}
$pilotExitCode = $LASTEXITCODE
exit $pilotExitCode
