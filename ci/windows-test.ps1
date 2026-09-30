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

# Run the actual creation script with WSL, registry and file operations mocked.
foreach ($version in 2, 1) {
    & {
        param([int]$Version)
        $testState = @{Installed=$false; Version=$Version; Events=[System.Collections.Generic.List[string]]::new()}
        function wsl.exe {
            $global:LASTEXITCODE = 0
            switch ($args[0]) {
                '--version' { 'WSL version: 3.0.1'; return }
                '--help' { '--from-file --location --name --set-sparse'; return }
                '--install' { $testState.Installed = $true; $testState.Events.Add('install') }
                '--set-version' {
                    if ($testState.Version -eq 2) { throw 'Redundant version conversion must not run.' }
                    Assert-Equal ($args -join ' ') '--set-version BootstrapTest 2' 'conversion arguments'
                    $testState.Events.Add('set-version')
                }
                '--terminate' {
                    Assert-Equal ($args -join ' ') '--terminate BootstrapTest' 'terminate arguments'
                    $testState.Events.Add('terminate')
                }
                '--manage' {
                    Assert-Equal ($args -join ' ') '--manage BootstrapTest --set-sparse true' 'sparse arguments'
                    $testState.Events.Add('set-sparse')
                }
                default { throw 'Unexpected WSL call.' }
            }
        }
        function Test-Path {
            param([string]$LiteralPath)
            return $LiteralPath -eq 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' -or
                $LiteralPath -eq 'D:\' -or $LiteralPath.EndsWith('ubuntu-24.04.5-wsl-amd64.wsl')
        }
        function Get-ChildItem {
            param([string]$LiteralPath)
            if ($LiteralPath -ne 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss') { throw 'Unexpected registry query.' }
            if ($testState.Installed) { [pscustomobject]@{PSPath='created'} }
        }
        function Get-ItemProperty {
            param([string]$LiteralPath)
            if ($LiteralPath -ne 'created') { throw 'Unexpected registry entry.' }
            [pscustomobject]@{DistributionName='BootstrapTest'; Version=$testState.Version; BasePath='D:\wsl\BootstrapTest'}
        }
        function New-Item { param($ItemType, [switch]$Force, $Path) }
        function Get-FileHash {
            param($Algorithm, $LiteralPath)
            [pscustomobject]@{Hash='bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e'}
        }
        function Invoke-WebRequest { throw 'Unexpected download.' }
        function Set-Content {
            param($LiteralPath, $Value, $Encoding, [switch]$NoNewline)
            Assert-Equal $LiteralPath 'D:\wsl\BootstrapTest\.bootstrap-image.sha256' 'marker path'
            Assert-Equal $Value 'bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e' 'marker hash'
            $testState.Events.Add('marker')
        }
        & "$PSScriptRoot/../windows/new-distro.ps1" -Name BootstrapTest
        $expectedEvents = if ($Version -eq 2) { 'install,marker,terminate,set-sparse' } else { 'install,set-version,marker,terminate,set-sparse' }
        Assert-Equal ($testState.Events -join ',') $expectedEvents "WSL $Version conversion decision and continuation"
    } $version
}
