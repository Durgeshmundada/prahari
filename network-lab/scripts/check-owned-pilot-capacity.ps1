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

$repoDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "../..")).Path
$reportDirectory = Join-Path $repoDirectory "artifacts/owned-pilot-host"
New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
if ((Get-Item -LiteralPath $reportDirectory).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Capacity report directory must not be a reparse point"
}

$registration = @(
    Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' |
        ForEach-Object { Get-ItemProperty $_.PSPath } |
        Where-Object { $_.DistributionName -eq $Distribution }
)
if ($registration.Count -ne 1) {
    throw "Cannot resolve one WSL registration for $Distribution"
}
if (-not (Test-Path -LiteralPath $registration[0].BasePath -PathType Container)) {
    throw "The registered WSL base path does not exist"
}
$backingRoot = Split-Path -Qualifier $registration[0].BasePath
$backingDrive = Get-PSDrive -Name $backingRoot.TrimEnd(':') -PSProvider FileSystem
$checkerSha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()

$stateFreeOutput = & wsl.exe -d $Distribution -u root -- df --output=avail -B1 -- $StateDirectory 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Cannot measure the owned pilot state filesystem"
}
$stateFreeLines = @($stateFreeOutput | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^\d+$' })
if ($stateFreeLines.Count -ne 1) {
    throw "Owned pilot state capacity output is invalid"
}
$stateFreeBytes = [uint64]$stateFreeLines[0]

$rootOptions = (& wsl.exe -d $Distribution -u root -- findmnt -n -o OPTIONS / 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($rootOptions)) {
    throw "Cannot inspect the owned pilot root filesystem"
}

$hostMinimumBytes = [uint64]$MinimumHostFreeGiB * 1GB
$stateMinimumBytes = [uint64]$MinimumStateFreeGiB * 1GB
$reasons = [System.Collections.Generic.List[string]]::new()
if ([uint64]$backingDrive.Free -lt $hostMinimumBytes) {
    $reasons.Add("wsl_backing_drive_below_minimum")
}
if ($stateFreeBytes -lt $stateMinimumBytes) {
    $reasons.Add("owned_state_filesystem_below_minimum")
}
if (-not (($rootOptions -split ',') -contains 'rw')) {
    $reasons.Add("owned_root_filesystem_not_read_write")
}

$report = [ordered]@{
    schema_version = "owned-pilot-capacity-v1"
    checked_at = [DateTimeOffset]::UtcNow.ToString("o")
    checker_sha256 = $checkerSha256
    distribution = $Distribution
    wsl_base_path = $registration[0].BasePath
    backing_root = $backingRoot
    backing_free_bytes = [uint64]$backingDrive.Free
    backing_minimum_bytes = $hostMinimumBytes
    state_directory = $StateDirectory
    state_free_bytes = $stateFreeBytes
    state_minimum_bytes = $stateMinimumBytes
    root_filesystem_options = $rootOptions
    healthy = $reasons.Count -eq 0
    reasons = @($reasons)
}

$reportPath = Join-Path $reportDirectory "capacity.json"
$temporaryPath = Join-Path $reportDirectory ("capacity-{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
try {
    [IO.File]::WriteAllText($temporaryPath, (($report | ConvertTo-Json -Depth 4) + [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryPath -Destination $reportPath -Force
} finally {
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

$report | ConvertTo-Json -Compress
if (-not $report.healthy) {
    exit 2
}
