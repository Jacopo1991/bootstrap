[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,47}$')][string]$Name,
      [string]$LogPath = 'C:\ProgramData\machine-bootstrap\compact.log')
. "$PSScriptRoot/common.ps1"

function Get-WslDistroState {
    param([Parameter(Mandatory)][string]$Name)
    $text = ((Invoke-Wsl -WslArgs @('--list', '--verbose')) -join "`n").Replace([string][char]0, '')
    $hasHeader = $false
    $targetState = $null
    foreach ($line in ($text -split "`r?`n")) {
        if ($line -match '^\s*NAME\s+STATE\s+VERSION\s*$') { $hasHeader = $true; continue }
        if (-not $hasHeader) { continue }
        $targetPattern = '^\s*(?:\*\s*)?' + [regex]::Escape($Name) + '\s+(Running|Stopped)\s+(\d+)\s*$'
        if ($line -match $targetPattern) {
            if ($null -ne $targetState) { throw "Multiple WSL list entries matched $Name." }
            $targetState = [pscustomobject]@{State=$Matches[1]; Version=[int]$Matches[2]}
        }
    }
    if (-not $hasHeader) { throw 'Could not parse output from wsl --list --verbose.' }
    return $targetState
}

function Invoke-WslCompactionCheck {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Name,
          [string]$LogPath = 'C:\ProgramData\machine-bootstrap\compact.log')

    $disks = @(Get-WslDisks | Where-Object { [string]::Equals($_.Name, $Name, [StringComparison]::OrdinalIgnoreCase) })
    if ($disks.Count -ne 1) { throw "Expected one VHDX record for $Name; found $($disks.Count)." }
    $disk = $disks[0]
    $stateRecord = Get-WslDistroState -Name $Name
    $logDirectory = Split-Path -Parent $LogPath
    if ($logDirectory) { New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null }

    $isRunningV2 = $null -ne $stateRecord -and $stateRecord.State -eq 'Running' -and $stateRecord.Version -eq 2 -and $disk.WslVersion -eq 2
    $state = if ($null -eq $stateRecord) { 'Unknown' } else { $stateRecord.State }
    $fileBytes = if ($null -eq $disk.FileBytes) { 'unknown' } else { [string]$disk.FileBytes }
    $baseLine = "CHECK: $($disk.Name) State=$state FileBytes=$fileBytes"
    Add-Content -LiteralPath $LogPath -Value $baseLine -Encoding utf8
    Write-Output $baseLine

    if (-not $isRunningV2) { return }
    $usage = Get-WslFilesystemUsage -Name $disk.Name
    $usedBytes = [long]$usage.UsedBytes
    $capBytes = [long]$usage.CapBytes
    $metricsLine = "USAGE: $($disk.Name) FileBytes=$fileBytes UsedBytes=$usedBytes CapBytes=$capBytes"
    Add-Content -LiteralPath $LogPath -Value $metricsLine -Encoding utf8
    Write-Output $metricsLine

    if ($null -eq $disk.FileBytes) { return }
    $gapBytes = [long]$disk.FileBytes - $usedBytes
    if ($gapBytes -gt 50GB -or [double]$disk.FileBytes -gt ([double]$capBytes * 0.9)) {
        $recommendation = "COMPACTION RECOMMENDED: run windows\compact-distro.ps1 -Name $($disk.Name)"
        Add-Content -LiteralPath $LogPath -Value $recommendation -Encoding utf8
        Write-Output $recommendation
    }
}

# Dot-sourcing exposes the checker for mocked tests without running it.
if ($MyInvocation.InvocationName -ne '.') {
    Invoke-WslCompactionCheck -Name $Name -LogPath $LogPath
}
