[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,47}$')][string]$Name,
      [ValidateRange(1, 65536)][int]$MaxSizeGB=800)
. "$PSScriptRoot/../windows/common.ps1"
Assert-BootstrapAdministrator
$disk = Get-CompactableWslDisk -Name $Name
Assert-WslCap -Name $Name -MaxSizeGB $MaxSizeGB | Out-Null
$testFile = '/var/tmp/bootstrap-compact-' + [Guid]::NewGuid().ToString('N') + '.dat'
$written = $false
try {
    Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'bash', '-c', 'test $(df --output=avail -B1 /var/tmp | tail -1) -gt 6442450944')
    $written = $true
    Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'dd', 'if=/dev/urandom', "of=$testFile", 'bs=1M', 'count=5120', 'iflag=fullblock', 'conv=fsync', 'status=progress')
    $peak = (Get-Item -LiteralPath $disk.Vhdx).Length
    Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'rm', '--', $testFile)
    $written = $false
    Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'sync')
    & "$PSScriptRoot/../windows/compact-distro.ps1" -Name $Name
    $after = (Get-Item -LiteralPath $disk.Vhdx).Length
    [pscustomobject]@{Distro=$Name; PeakFileBytes=$peak; AfterFileBytes=$after; ReclaimedBytes=$peak-$after}
    if ($peak - $after -lt 4GB) { throw 'FAIL: compaction did not shrink the VHDX file by at least 4 GiB.' }
    Write-Output 'PASS: filesystem cap and 5 GiB write/delete/compact check.'
} finally {
    if ($written) { Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'rm', '-f', '--', $testFile) }
}
