[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{0,47}$')][string]$Name)
. "$PSScriptRoot/../windows/common.ps1"
Assert-WslVersion
$disk = @(Get-WslDisks | Where-Object Name -eq $Name)
if ($disk.Count -ne 1 -or $disk[0].WslVersion -ne 2) { throw 'Select an existing WSL 2 distro.' }
$vhd = $disk[0].Vhdx
$testFile = '/var/tmp/bootstrap-sparse-' + [Guid]::NewGuid().ToString('N') + '.dat'
$written = $false
try {
    Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'bash', '-c', 'test $(df --output=avail -B1 /var/tmp | tail -1) -gt 6442450944')
    # /dev/urandom prevents zero-block optimization from faking growth.
    $written = $true
    Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'dd', 'if=/dev/urandom', "of=$testFile", 'bs=1M', 'count=5120', 'iflag=fullblock', 'conv=fsync', 'status=progress')
    Invoke-Wsl -WslArgs @('--terminate', $Name)
    $peak = [BootstrapDiskSize]::Allocated($vhd)
    Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'rm', '--', $testFile)
    $written = $false
    Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'sync')
    Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'fstrim', '/')
    Invoke-Wsl -WslArgs @('--terminate', $Name)
    $deadline = (Get-Date).AddMinutes(3)
    do {
        $after = [BootstrapDiskSize]::Allocated($vhd)
        if ($peak - $after -ge 4GB) { break }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)
    [pscustomobject]@{ Distro = $Name; PeakAllocatedBytes = $peak; AfterAllocatedBytes = $after; ReclaimedBytes = $peak - $after }
    if ($peak - $after -lt 4GB) { throw 'FAIL: sparse reclaim did not release at least 4 GiB after deleting the 5 GiB file.' }
    Write-Output 'PASS: VHDX allocated disk space shrank after deletion/TRIM.'
} finally {
    if ($written) { Invoke-Wsl -WslArgs @('-d', $Name, '-u', 'root', '--', 'rm', '-f', '--', $testFile) }
}
