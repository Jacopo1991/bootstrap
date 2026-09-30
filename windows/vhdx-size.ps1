[CmdletBinding()]
param()
. "$PSScriptRoot/common.ps1"
Get-WslDisks | Select-Object Name, WslVersion, Vhdx, FileBytes, AllocatedBytes |
    Format-Table -AutoSize
