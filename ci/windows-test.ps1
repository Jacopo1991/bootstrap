Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
    throw 'These regression tests must run in Windows PowerShell 5.1.'
}
. "$PSScriptRoot/../windows/common.ps1"

function Assert-Equal {
    param($Actual, $Expected, [string]$Label)
    if ($Actual -cne $Expected) { throw "$Label`: expected '$Expected', got '$Actual'." }
    Write-Output "PASS: $Label"
}
Assert-Equal (ConvertTo-WslBasePath '\\?\D:\wsl\X') 'D:\wsl\X' 'extended drive path'
Assert-Equal (ConvertTo-WslBasePath 'D:\wsl\X') 'D:\wsl\X' 'plain drive path'
Assert-Equal (ConvertTo-WslBasePath '%LOCALAPPDATA%\X') ([IO.Path]::GetFullPath($env:LOCALAPPDATA + '\X')) 'environment expansion'
Assert-Equal (ConvertTo-WslBasePath '\\?\UNC\srv\share\X') '\\srv\share\X' 'extended UNC path'
Assert-Equal (@(ConvertTo-WslBasePath $null).Count) 0 'null path skipped'

# Registry fixtures exercise missing BasePath under strict mode without host changes.
function Test-Path {
    param([string]$LiteralPath)
    return $LiteralPath -eq 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
}
function Get-ChildItem {
    param([string]$LiteralPath)
    if ($LiteralPath -ne 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss') { throw 'Unexpected registry query.' }
    foreach ($name in 'missing', 'null', 'valid') { [pscustomobject]@{PSPath=$name} }
}
function Get-ItemProperty {
    param([string]$LiteralPath)
    switch ($LiteralPath) {
        'missing' { [pscustomobject]@{DistributionName='Missing'; Version=2} }
        'null' { [pscustomobject]@{DistributionName='Null'; Version=2; BasePath=$null} }
        'valid' { [pscustomobject]@{DistributionName='Valid'; Version=2; BasePath='\\?\C:\wsl\X'} }
        default { throw 'Unexpected registry entry.' }
    }
}
$results = @(Get-WslDisks 3>&1)
$warnings = @($results | Where-Object { $_ -is [System.Management.Automation.WarningRecord] })
$disks = @($results | Where-Object { $_ -isnot [System.Management.Automation.WarningRecord] })
Assert-Equal $warnings.Count 2 'missing and null BasePath warn'
Assert-Equal $disks.Count 1 'missing and null registry entries skipped'
Assert-Equal $disks[0].BasePath 'C:\wsl\X' 'registry BasePath normalized'
Assert-Equal $disks[0].Vhdx 'C:\wsl\X\ext4.vhdx' 'normalized VHDX path'
